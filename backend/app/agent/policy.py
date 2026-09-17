"""L2エージェントのサンドボックス・権限ポリシー(#45, 設計書§8.5)。

**安全性を「エージェントが賢いこと」に依存させず、構造として担保する**
(§8.4/§8.5、R-11)。単一のポリシー表からClaude Agent SDK用のhooks設定と
OpenCode用の設定断片の両方を生成し、両プロバイダで挙動が乖離しないようにする。

実際に`ClaudeAgentOptions`/`opencode.json`を構築してセッションを起動するのは
#48(ClaudeAgentProvider)/#52(OpenCodeProvider)の担当。ここでは「渡すだけで
済む」形の値・関数(`build_claude_hooks`/`build_opencode_tools_config`)を
提供する。`timeout_sec`/`max_tokens_budget`の実際の非同期タイムアウト・
予算超過時の中断処理は、生きているセッション(asyncioイベントループ)を
握っているプロバイダ側の責務であり、ここでは判定用の純粋関数
(`exceeds_token_budget`)のみを提供する。
"""

from __future__ import annotations

import contextlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from claude_agent_sdk import HookMatcher

from app.agent.audit import AuditEntry, append_audit_entry

# NFR-14: シェルコマンドのホワイトリスト(設計書§8.5)。`rm -rf`/`curl`/`git`/
# パッケージインストール等は不許可。
#
# 設計書は例として`python`も挙げているが、意図的に含めない(#45 Gate2レビュー
# 指摘・2巡目 HIGH): pythonは汎用インタプリタであり、フラグ(`-c`/`-i`/`-m`)を
# 拒否しスクリプトパスをworkspace配下に限定しても、その連結表記
# (`-c"..."`/`-mモジュール名`、shlexの1トークン化により完全一致の拒否判定を
# 素通りする)による回避や、workspace配下の許可された.pyファイル自身が
# `open('/etc/passwd')`のような任意のファイルI/O・ネットワークアクセスを
# 行うことまでは防げない。引数検証という名前ベースの制約では、汎用言語の
# 実行そのものを構造的に安全にはできないと判断し、ホワイトリストから外す
# (OSレベルのプロセスサンドボックス等、別の強制力が無い限り復活させない)。
SHELL_COMMAND_WHITELIST: frozenset[str] = frozenset({"ls", "cat", "grep"})

# ネットワーク系ツールは静的に無効化する(hookではなくClaudeAgentOptions.
# disallowed_toolsへそのまま渡す、設計書§8.5のpseudocode通り)。
DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")

# R-10: 同一検証違反が続いた場合の強制打ち切り閾値。
MAX_CONSECUTIVE_SCORE_APPLY_OPS_FAILURES: Final = 3

_SCORE_APPLY_OPS_TOOL_NAME = "mcp__score__score_apply_ops"

# NFR-14: 複数コマンドの連結・置換に使われうるシェルメタ文字。ホワイトリストの
# 判定は「先頭コマンド名だけ見る」のではなく、これらの文字が1つでも含まれて
# いれば無条件に拒否する(パースして安全性を判断するより、保守的に全拒否する
# 方がセキュリティ境界として安全なため)。改行/復帰(`\n`/`\r`)もシェルにとっては
# コマンド区切りとして働く(#45 Gate2レビュー指摘・1巡目 HIGH: これが無いと
# "ls\ncurl ..."のような改行区切りでの連結を素通ししてしまっていた)。
_SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "&", "\n", "\r")

# NFR-14: cat/grep/lsは引数(読み取り対象パス)を検証しないと、workspace外の
# 任意ファイル読み取り(例: `cat /etc/passwd`)に使われてしまう(#45 Gate2
# レビュー指摘・1巡目 HIGH)。パスらしき引数(`/`を含む、または`.`/`..`)は
# workspace配下のもののみ許可する。`/`を含まない裸のトークン(例: grepの
# 検索パターン文字列)は、実行時のcwdがworkspace配下である前提
# (#48が`ClaudeAgentOptions(cwd=str(task.workspace))`を設定する設計、
# 設計書§8.3のpseudocode参照)の下では単体でworkspace外を参照できないため
# 検証対象外とする。
_PATH_SENSITIVE_EXECUTABLES = frozenset({"cat", "grep", "ls"})


@dataclass
class PolicyContext:
    """1エージェントrunにつき1つ。#48/#52がプロバイダ構築時に作る。

    `consecutive_score_apply_ops_failures`はrun中に変化する唯一の状態のため
    frozenにしない(#42の`AgentTask`等、他のデータクラスは全てfrozen)。
    """

    workspace: Path
    run_id: str
    project_id: str
    consecutive_score_apply_ops_failures: int = 0


def _looks_like_path(arg: str) -> bool:
    """引数がファイル/ディレクトリパスらしいかを判定する(フラグ・裸の

    トークンとの区別用)。`/`を含む(絶対パス・相対ディレクトリ指定)、または
    `.`/`..`そのものであればパスとみなす。
    """
    return "/" in arg or arg in (".", "..")


def is_allowed_command(command: str, workspace: Path) -> bool:
    """NFR-14: シェルコマンドのホワイトリスト判定。

    シェルメタ文字(改行含む)を含む場合は複数コマンドの連結(ホワイトリストの
    回避)を許す可能性があるため無条件に拒否する。それ以外は`shlex.split`で
    トークン化し、先頭の実行ファイル名(パス部分を除いた basename)を
    `SHELL_COMMAND_WHITELIST`と照合する。

    `posix=False`でトークン化する: 本プロジェクトはWindows専用(NFR-08′)の
    ため、コマンド文字列にはバックスラッシュ区切りのWindowsパス
    (`C:\\Users\\...`)が渡されうる。既定のPOSIXモードだとバックスラッシュを
    エスケープ文字として解釈し`C:\\Users\\foo`が`C:Usersfoo`に化けてしまい、
    後段のworkspace境界チェックが正しいパスを見られなくなる(#45 Gate2
    レビュー指摘・2巡目、Windows実行のCIで発覚)。

    実行ファイル名の一致だけでは不十分なコマンド(`_PATH_SENSITIVE_EXECUTABLES`
    参照)について、追加で引数を検証する(#45 Gate2レビュー指摘・1巡目 HIGH):
    cat/grep/lsは`/`を含む引数(パスらしきもの)をworkspace配下のみ許可する。
    """
    if any(meta in command for meta in _SHELL_METACHARACTERS):
        return False
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        # 引用符の対応が取れない等、shlexが解釈できない不正な文字列は拒否する。
        return False
    if not tokens:
        return False
    executable = Path(tokens[0]).name
    if executable not in SHELL_COMMAND_WHITELIST:
        return False

    args = tokens[1:]
    if executable in _PATH_SENSITIVE_EXECUTABLES:
        return all(not _looks_like_path(arg) or within_workspace(arg, workspace) for arg in args)
    return True


def within_workspace(path: str, workspace: Path) -> bool:
    """NFR-13: 書き込み先が`workspace`(agent_workspace/{run_id}/)配下かを判定する。

    `Path.resolve()`で正規化した上で`is_relative_to()`比較し、`../`等の
    パストラバーサルを防ぐ。`score/current.json`等、workspace外の全パスは
    この関数だけで自動的に拒否される(スコアパス固有の特別扱いは不要)。
    """
    try:
        resolved_path = Path(path).resolve()
        resolved_workspace = workspace.resolve()
    except OSError:
        # 解決不能なパス(壊れたシンボリックリンク等)は安全側に倒して拒否する。
        return False
    return resolved_path.is_relative_to(resolved_workspace)


def _record_pretooluse_decision(
    ctx: PolicyContext,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    decision: Literal["allow", "deny"],
    reason: str | None,
) -> None:
    # 監査ログの書き込み失敗でポリシー判定自体を失敗させない(#41/#43の
    # _record_ai_decisionと同じ「主処理を優先する」方針)。
    with contextlib.suppress(OSError):
        append_audit_entry(
            ctx.workspace,
            AuditEntry(
                run_id=ctx.run_id,
                project_id=ctx.project_id,
                tool_name=tool_name,
                tool_input=tool_input,
                decision=decision,
                reason=reason,
            ),
        )


async def guard_bash(
    input_data: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
) -> dict[str, Any]:
    """PreToolUseフック(matcher="Bash")。ホワイトリスト外なら拒否+監査記録する。"""
    ctx: PolicyContext = context["ctx"]
    command = input_data.get("tool_input", {}).get("command", "")
    if is_allowed_command(command, ctx.workspace):
        _record_pretooluse_decision(
            ctx,
            tool_name="Bash",
            tool_input=input_data.get("tool_input", {}),
            decision="allow",
            reason=None,
        )
        return {}
    reason = f"許可されていないコマンドです: {command}"
    _record_pretooluse_decision(
        ctx,
        tool_name="Bash",
        tool_input=input_data.get("tool_input", {}),
        decision="deny",
        reason=reason,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def guard_write(
    input_data: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
) -> dict[str, Any]:
    """PreToolUseフック(matcher="Write|Edit")。workspace外への書き込みを拒否する。

    拒否理由に「楽譜の変更はscore_apply_opsを使ってください」という誘導を
    含める(設計書§8.5のpseudocode通り)。
    """
    ctx: PolicyContext = context["ctx"]
    tool_name = input_data.get("tool_name", "Write")
    tool_input = input_data.get("tool_input", {})
    path = tool_input.get("file_path", "")
    if within_workspace(path, ctx.workspace):
        _record_pretooluse_decision(
            ctx, tool_name=tool_name, tool_input=tool_input, decision="allow", reason=None
        )
        return {}
    reason = "楽譜の変更は mcp__score__score_apply_ops を使ってください"
    _record_pretooluse_decision(
        ctx, tool_name=tool_name, tool_input=tool_input, decision="deny", reason=reason
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def audit_post_tool_use(
    input_data: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
) -> dict[str, Any]:
    """PostToolUseフック(matcher=None、全ツール対象)。

    全ツール呼び出しの結果を監査ログに記録する(NFR-15/FR-22)。加えて
    `score_apply_ops`の結果が`{"ok": false, ...}`の場合のみ
    `PolicyContext.consecutive_score_apply_ops_failures`をインクリメントし、
    `MAX_CONSECUTIVE_SCORE_APPLY_OPS_FAILURES`に達したら`{"continue": False,
    "stopReason": ...}`を返してセッションを強制終了する(R-10)。`ok: true`
    が返れば streak をリセットする。
    """
    ctx: PolicyContext = context["ctx"]
    tool_name = input_data.get("tool_name", "")
    tool_response = input_data.get("tool_response")

    with contextlib.suppress(OSError):
        append_audit_entry(
            ctx.workspace,
            AuditEntry(
                run_id=ctx.run_id,
                project_id=ctx.project_id,
                tool_name=tool_name,
                tool_input=input_data.get("tool_input", {}),
                decision="allow",
                result=tool_response,
            ),
        )

    if tool_name != _SCORE_APPLY_OPS_TOOL_NAME:
        return {}

    ok = isinstance(tool_response, dict) and tool_response.get("ok") is True
    if ok:
        ctx.consecutive_score_apply_ops_failures = 0
        return {}

    ctx.consecutive_score_apply_ops_failures += 1
    if ctx.consecutive_score_apply_ops_failures < MAX_CONSECUTIVE_SCORE_APPLY_OPS_FAILURES:
        return {}
    return {
        "continue": False,
        "stopReason": (
            f"score_apply_opsの検証違反が{MAX_CONSECUTIVE_SCORE_APPLY_OPS_FAILURES}回"
            "連続したため、エージェントの実行を強制終了しました。"
        ),
    }


def build_claude_hooks(ctx: PolicyContext) -> dict[str, list[HookMatcher]]:
    """#48が`ClaudeAgentOptions(hooks=...)`へそのまま渡す値を組み立てる。

    `ctx`を辞書経由でフックへ渡す(SDKのフック呼び出し規約は
    `(input_data, tool_use_id, context)`の3引数固定で、追加引数を挟めない
    ため、`context`引数に`{"ctx": ctx}`を混ぜて渡すクロージャでラップする)。
    """

    async def _guard_bash(input_data, tool_use_id, context):
        return await guard_bash(input_data, tool_use_id, {**context, "ctx": ctx})

    async def _guard_write(input_data, tool_use_id, context):
        return await guard_write(input_data, tool_use_id, {**context, "ctx": ctx})

    async def _audit_post_tool_use(input_data, tool_use_id, context):
        return await audit_post_tool_use(input_data, tool_use_id, {**context, "ctx": ctx})

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[_guard_bash]),
            HookMatcher(matcher="Write", hooks=[_guard_write]),
            HookMatcher(matcher="Edit", hooks=[_guard_write]),
        ],
        "PostToolUse": [
            HookMatcher(matcher=None, hooks=[_audit_post_tool_use]),
        ],
    }


def build_opencode_tools_config() -> dict[str, Any]:
    """OpenCode用の`opencode.json`"tools"セクション相当の設定断片(推定実装)。

    ローカルに`opencode`パッケージが無く、設計書にもJSON具体例が無いため、
    一般に知られるbool値によるツール無効化形式で実装する。**実際の
    OpenCodeの挙動・スキーマによる検証はまだ行っていない — #52
    (OpenCodeProvider実装)の担当として残す。**

    bash/write/editはClaude側と同じ意図(許可はするが制約付き)で`True`とする
    (#45 Gate2レビュー指摘・1巡目 MIDDLE: 一律`False`で全無効化すると
    OpenCode版エージェントが一切作業できず、「単一のポリシー表から両
    プロバイダを生成し挙動を乖離させない」という本モジュールの方針に反する)。
    **ただし`opencode.json`の`"tools"`は静的なon/off切り替えのみで、
    Claude側の`PreToolUse`フックのような動的な引数検証(シェルコマンドの
    ホワイトリスト、workspace境界チェック)を表現できない。** そのため
    bash/write/editを有効化しても、Claude側と同等のNFR-13/14の実効的な
    強制力は無い — #52はこの設定だけに頼らず、別途動的な検証手段(例:
    stdio_serverでの追加チェックや、opencode自体のプロセスサンドボックス)
    を用意する必要がある。
    """
    return {
        "tools": {
            "bash": True,
            "write": True,
            "edit": True,
            "webfetch": False,
        }
    }


def exceeds_token_budget(usage_tokens: int, budget: int) -> bool:
    """3重防御のうちトークン予算の判定のみを行う純粋関数。

    実際の累積カウントと打ち切り処理(非同期セッションを握る側の責務)は
    #48/#52が行う。
    """
    return usage_tokens > budget
