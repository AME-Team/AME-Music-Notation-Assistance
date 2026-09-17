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
from typing import Any, Final

from claude_agent_sdk import HookMatcher

from app.agent.audit import AuditEntry, append_audit_entry

# NFR-14: シェルコマンドのホワイトリスト(設計書§8.5)。`rm -rf`/`curl`/`git`/
# パッケージインストール等は不許可。
SHELL_COMMAND_WHITELIST: frozenset[str] = frozenset({"python", "python3", "ls", "cat", "grep"})

# ネットワーク系ツールは静的に無効化する(hookではなくClaudeAgentOptions.
# disallowed_toolsへそのまま渡す、設計書§8.5のpseudocode通り)。
DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")

# R-10: 同一検証違反が続いた場合の強制打ち切り閾値。
MAX_CONSECUTIVE_SCORE_APPLY_OPS_FAILURES: Final = 3

_SCORE_APPLY_OPS_TOOL_NAME = "mcp__score__score_apply_ops"

# NFR-14: 複数コマンドの連結・置換に使われうるシェルメタ文字。ホワイトリストの
# 判定は「先頭コマンド名だけ見る」のではなく、これらの文字が1つでも含まれて
# いれば無条件に拒否する(パースして安全性を判断するより、保守的に全拒否する
# 方がセキュリティ境界として安全なため)。
_SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "&")


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


def is_allowed_command(command: str) -> bool:
    """NFR-14: シェルコマンドのホワイトリスト判定。

    シェルメタ文字を含む場合は複数コマンドの連結(ホワイトリストの回避)を
    許す可能性があるため無条件に拒否する。それ以外は`shlex.split`で
    トークン化し、先頭の実行ファイル名(パス部分を除いた basename)を
    `SHELL_COMMAND_WHITELIST`と照合する。
    """
    if any(meta in command for meta in _SHELL_METACHARACTERS):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        # 引用符の対応が取れない等、shlexが解釈できない不正な文字列は拒否する。
        return False
    if not tokens:
        return False
    executable = Path(tokens[0]).name
    return executable in SHELL_COMMAND_WHITELIST


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
    decision: str,
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
                decision=decision,  # type: ignore[arg-type]
                reason=reason,
            ),
        )


async def guard_bash(
    input_data: dict[str, Any], tool_use_id: str | None, context: dict[str, Any]
) -> dict[str, Any]:
    """PreToolUseフック(matcher="Bash")。ホワイトリスト外なら拒否+監査記録する。"""
    ctx: PolicyContext = context["ctx"]
    command = input_data.get("tool_input", {}).get("command", "")
    if is_allowed_command(command):
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
    """
    return {
        "tools": {
            "bash": False,
            "write": False,
            "edit": False,
            "webfetch": False,
        }
    }


def exceeds_token_budget(usage_tokens: int, budget: int) -> bool:
    """3重防御のうちトークン予算の判定のみを行う純粋関数。

    実際の累積カウントと打ち切り処理(非同期セッションを握る側の責務)は
    #48/#52が行う。
    """
    return usage_tokens > budget
