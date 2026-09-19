"""ClaudeAgentProvider(#48, 設計書§8.2/§8.3/§8.5/§8.9)— L2の既定プロバイダ。

`claude-agent-sdk`(0.2.139、`ClaudeSDKClient`)をインプロセスで呼ぶ。score-mcpも
別プロセスを起動せず`create_sdk_mcp_server`でインプロセス保持する(§8.3「score-mcp
を別プロセスとして起動する必要がない」)。`agent/policy.py`(#45)の`PreToolUse`/
`PostToolUse`フックをそのまま`ClaudeAgentOptions.hooks`へ渡すことで、安全性を
「エージェントが賢いこと」に依存させず構造として担保する(§8.4/§8.5)— そのため
CLIの対話的な許可プロンプト(未接続のヘッドレス実行では応答不能)は
`permission_mode="bypassPermissions"`で迂回し、実際の許可/拒否判定は
PreToolUseフックのみに委ねる(フックの deny 決定は`permission_mode`に関わらず
常に優先される、SDKの仕様)。

`AgentTask.mcp_servers`の各`McpServerSpec`は本プロバイダでは`kind="in_process"`
のみ対応する(`kind="stdio"`はOpenCodeProvider(#52)向け)。`config`は
`{"workspace_dir": <str|Path>}`(アプリ全体のワークスペースルート、
`Settings.workspace_dir`相当)を必須キーとして要求する — `mcp/stdio_server.py`の
`AME_WORKSPACE_DIR`環境変数と同じ役割を、環境変数ではなくこの`config`辞書経由で
渡す(インプロセスのため環境変数を経由する必要が無い)。`project_id`は
`AgentTask.project_id`から補う。`run_id`は`config`に`"run_id"`キーがあれば
それを`start()`が採番するprovider内部run_id自体として採用し、無ければ
`start()`が`uuid.uuid4()`で独自に採番する(`_resolve_run_id`参照)。
呼び出し元(#49 `AgentRunManager`)が公開run_id(DB/URLで使うID)を
`config["run_id"]`として渡すことで、`score_apply_ops`が書き込むstaging先
(`score/staging/{run_id}.json`)を`AgentRunService.accept`/`reject`/`get_diff`
(#46)が探すパスと一致させられる — これを渡さないと、provider が独自採番した
別のrun_idでstagingファイルが書かれ、承認フローが永遠にステージング済みの
変更を見つけられなくなる(#50の実機テストで発見した実バグ)。

リトライ方針(§8.9「プロバイダAPIエラー: 指数バックオフでリトライ(3回)」):
**接続確立前**(`ClaudeSDKClient.connect()`が最初のメッセージを1件も返す前)に
`ClaudeSDKError`が発生した場合のみ、新しいクライアントで最大3回まで再試行する。
1件でもメッセージを受信した後の失敗は再試行しない — score_apply_opsは
このrun専用のstagingファイルへ書くため、部分的に進行した状態から接続をやり直すと
オペレーションが重複適用される恐れがあるため(冪等性が無い)。`CLINotFoundError`
(CLIバイナリ自体が無い)は再試行しても解決しないため即座に失敗させる。
全試行失敗時はrunを`failed`にし、L1/L0の結果(`score/current.json`)には
一切触れない(本プロバイダは`score/staging/{run_id}.json`しか書かないため、
これは構造的に保証される)。

タイムアウト(`AgentTask.timeout_sec`)は`connect()`(CLIサブプロセスの
ハンドシェイク)および各メッセージ受信の`await`ごとに`asyncio.wait_for`で
強制する(単一の長時間ハングにも対応するため、ループ全体を1回`wait_for`で
包むのではなく個々の`await`単位で包む)。接続リトライのバックオフ待機も
残りdeadline以下に切り詰め、deadline切れなら試行回数の上限未到達でも
リトライせず`truncated`で終了する。トークン予算
(`AgentTask.max_tokens_budget`)は直近の`AssistantMessage.usage`(その時点までの
累積コンテキスト使用量)で`policy.exceeds_token_budget`を毎ターン判定する。
両者とも超過時はrunを`truncated`にする(§8.9「それまでにステージングされた
変更は破棄せず提示する」— 本プロバイダはstagingファイルに触れないため、
ステージング済みの内容は自然に保持される)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.agent.mcp.sdk_adapter import SERVER_NAME, build_sdk_mcp_server
from app.agent.mcp.tools import ToolContext
from app.agent.policy import (
    DISALLOWED_TOOLS,
    PolicyContext,
    build_claude_hooks,
    exceeds_token_budget,
)
from app.agent.provider import (
    AgentEvent,
    AgentEventKind,
    AgentResult,
    AgentRunHandle,
    AgentRunNotFoundError,
    AgentRunStatus,
    AgentTask,
    TokenUsage,
)
from app.config import resolve_api_key

_MAX_CONNECT_ATTEMPTS: Final = 3
_BACKOFF_BASE_SEC: Final = 1.0
_ANTHROPIC_KEY_PROVIDER: Final = "anthropic"
_SCORE_APPLY_OPS_TOOL_NAME: Final = f"mcp__{SERVER_NAME}__score_apply_ops"


class ClaudeAgentProviderError(RuntimeError):
    """タスク構成エラー(未対応の`mcp_servers.kind`、`config`欠落等)。

    `AgentRunNotFoundError`とは異なりrun開始前の構成不備なので、`start()`が
    `_build_mcp_servers()`を(runを`self._runs`へ登録する前に)呼んで直接
    送出する。Gate2レビュー指摘: 以前は`stream()`内(=run登録後)で構成を
    組み立てており、構成不備時にrunが`status="running"`のまま残る状態機械の
    穴があった。
    """


@dataclass
class _RunState:
    run_id: str
    task: AgentTask
    mcp_servers: dict[str, Any]
    status: AgentRunStatus = "running"
    events: list[AgentEvent] | None = None
    cancel_requested: bool = False
    # トークン予算超過による`client.interrupt()`起因の中断であることを示す
    # (Gate2レビュー指摘: これが無いと、超過後にSDKが返す`aborted_streaming`/
    # `aborted_tools`をユーザーによる`cancel_requested`と区別できず、
    # §8.9が定める`truncated`ではなく`cancelled`になってしまっていた)。
    budget_exceeded: bool = False
    client: ClaudeSDKClient | None = None
    turns: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    staged_ops_count: int = 0
    error: str | None = None


def _resolve_run_id(task: AgentTask) -> str | None:
    """`McpServerSpec.config["run_id"]`(呼び出し元が公開run_idを明示した場合)を

    優先的なprovider内部run_idとして使う。指定が無ければ`None`を返し、
    呼び出し元(`start()`)が独自に採番する。

    #50の実機テストで発見: `score_apply_ops`が書き込むstaging先
    (`score/staging/{run_id}.json`)は`ToolContext.run_id`由来であり、これは
    従来常に`start()`が`str(uuid.uuid4())`で独自採番した値だった。#49の
    `AgentRunManager`は自らが発行した公開run_id(DB/URLで使うID)とは別の
    このprovider内部run_idを`AgentEvent.run_id`の書き換えでは吸収していたが、
    staging先ファイル名の食い違いまでは吸収しておらず、
    `AgentRunService.accept`/`reject`/`get_diff`(#46)が公開run_idベースで
    `score/staging/{公開run_id}.json`を探すため、実際に書き込まれた
    `score/staging/{provider内部run_id}.json`を永遠に見つけられず、
    `score_apply_ops`が成功していても承認フローが機能しない状態になっていた。
    `AgentRunManager`が`config={"workspace_dir": ..., "run_id": 公開run_id}`を
    渡すようにし(`agent_run_manager.py`参照)、ここでそれを`start()`が採番する
    run_id自体として採用することで一致させる。

    走査対象は`kind="in_process"`かつ`name=SERVER_NAME`("score")のspecに限定する
    (Gate2レビュー指摘・MIDDLE: staging先ファイル名の一致は score サーバの
    `ToolContext.run_id`にのみ依存するため、無条件に「最初に見つかった
    config["run_id"]」を採用すると、将来score以外のspecが混在した場合や
    順序が変わった場合に無関係な値を拾ってこのバグが再発しうる)。

    **契約(Gate2レビュー指摘・LOW)**: `config["run_id"]`を指定する呼び出し元は、
    プロセス内でその値が(同時に生存している他のrunと)一意であることを保証する
    責任を負う。ここで採用したrun_idが既に`self._runs`に存在する場合、
    `start()`は`ClaudeAgentProviderError`を送出してfail-fastする
    (サイレントな状態上書きを避けるため)。
    """
    for spec in task.mcp_servers:
        if spec.kind != "in_process" or spec.name != SERVER_NAME:
            continue
        run_id = spec.config.get("run_id")
        if run_id:
            return str(run_id)
    return None


def _build_mcp_servers(task: AgentTask, run_id: str) -> dict[str, Any]:
    """`AgentTask.mcp_servers`(`kind="in_process"`のみ)を`McpSdkServerConfig`へ変換する。"""
    servers: dict[str, Any] = {}
    for spec in task.mcp_servers:
        if spec.kind != "in_process":
            raise ClaudeAgentProviderError(
                f"ClaudeAgentProvider supports only kind='in_process' mcp servers, "
                f"got kind={spec.kind!r} for server {spec.name!r}"
            )
        workspace_dir = spec.config.get("workspace_dir")
        if not workspace_dir:
            raise ClaudeAgentProviderError(
                f"mcp server spec {spec.name!r} is missing required config key 'workspace_dir'"
            )
        ctx = ToolContext(
            workspace_dir=Path(workspace_dir), project_id=task.project_id, run_id=run_id
        )
        servers[spec.name] = build_sdk_mcp_server(ctx)
    return servers


def _resolve_env() -> dict[str, str]:
    """NFR-10: 環境変数/OSキーチェーンからAPIキーを解決し、あれば子プロセスへ渡す。

    `claude-agent-sdk`は同梱のClaude Code CLIをサブプロセスとして起動する
    (親プロセスの環境を継承した上で`ClaudeAgentOptions.env`をマージする)。CLIが
    既に(サブスクリプション/OAuthで)認証済みであれば`ANTHROPIC_API_KEY`が
    無くても動作するため(#104と同じ思想)、キーが見つからない場合は何も設定せず
    CLI自身の認証状態に委ねる。
    """
    api_key = resolve_api_key(_ANTHROPIC_KEY_PROVIDER)
    return {"ANTHROPIC_API_KEY": api_key} if api_key else {}


def _build_options(task: AgentTask, run_id: str, mcp_servers: dict[str, Any]) -> ClaudeAgentOptions:
    policy_ctx = PolicyContext(workspace=task.workspace, run_id=run_id, project_id=task.project_id)
    return ClaudeAgentOptions(
        cwd=str(task.workspace),
        mcp_servers=mcp_servers,
        allowed_tools=list(task.allowed_tools),
        disallowed_tools=list(DISALLOWED_TOOLS),
        max_turns=task.max_turns,
        model=task.model,
        # ヘッドレス実行には対話的な許可プロンプトが無いため迂回する(モジュール
        # docstring参照)。実際の許可/拒否は`hooks`のPreToolUseが構造的に担う。
        permission_mode="bypassPermissions",
        hooks=build_claude_hooks(policy_ctx),
        env=_resolve_env(),
    )


def _token_usage_from_dict(usage: dict[str, Any] | None) -> TokenUsage:
    usage = usage or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
    )


def _extract_tool_result_payload(content: str | list[dict[str, Any]] | None) -> Any:
    """`ToolResultBlock.content`からテキストを取り出し、JSONならparseして返す。

    score-mcpの各ツールは`_to_content`(`sdk_adapter.py`)で`{"content":
    [{"type":"text","text": ...}]}`形式を返すため、通常はここでJSONへ復元できる。
    """
    text: str | None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t) if texts else None
    else:
        text = None
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _resolve_interrupted_status(run: _RunState, *, default: AgentRunStatus) -> AgentRunStatus:
    """`cancel_requested`/`budget_exceeded`を`default`より優先する共通ヘルパ。

    Gate2レビュー指摘: `_map_terminal_status`とStopAsyncIterationフォールバック
    だけに優先順位を適用しても、タイムアウト系の終端経路(`connect()`の
    タイムアウト・deadline切れ・メッセージ受信のタイムアウト)は独自に
    `"truncated"`を決め打ちしていたため、`cancel()`呼び出し後でもタイムアウトが
    先に発火すると状態が"truncated"へ巻き戻っていた。全ての「タイムアウト/
    中断」系の終端イベント生成箇所をこのヘルパ経由に統一する。
    """
    if run.cancel_requested:
        return "cancelled"
    if run.budget_exceeded:
        return "truncated"
    return default


def _map_terminal_status(
    result: ResultMessage, *, cancel_requested: bool, budget_exceeded: bool
) -> tuple[AgentRunStatus, str | None]:
    # `cancel_requested`/`budget_exceeded`を`terminal_reason`の判定より先に見る
    # (Gate2レビュー指摘): どちらも`client.interrupt()`を呼ぶため、SDKが返す
    # `terminal_reason`は両ケースとも同じ"aborted_streaming"/"aborted_tools"に
    # なりうる。予算超過は§8.9が定める"truncated"であり、ユーザーキャンセルの
    # "cancelled"とは区別しなければならない。
    if cancel_requested:
        return "cancelled", None
    if budget_exceeded:
        return "truncated", None
    if result.terminal_reason in ("aborted_streaming", "aborted_tools"):
        return "cancelled", None
    if result.is_error:
        error = result.result or "; ".join(result.errors or []) or f"subtype={result.subtype}"
        return "failed", error
    if result.terminal_reason not in (None, "completed"):
        return "truncated", None
    return "completed", None


class ClaudeAgentProvider:
    """`AgentProvider`Protocolの実装(設計書§8.2/§8.3)。

    `start()`はrunを登録するのみ。実際にSDKクライアントを接続し駆動するのは
    `stream()`(遅延実行、`DummyAgentProvider`と同じ設計 — `agent_contract.py`の
    「`cancel()`を`stream()`消費前に呼んでも反映される」という契約を、実行を
    `stream()`側に閉じ込めることで単純に満たせるため)。`stream()`を最後まで
    消費すると結果を`run.events`へキャッシュし、以後の再呼び出しは実SDKを
    再度叩かずキャッシュを再生する。
    """

    name = "claude"

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}

    async def start(self, task: AgentTask) -> AgentRunHandle:
        run_id = _resolve_run_id(task) or str(uuid.uuid4())
        # Gate2レビュー指摘(LOW): `config["run_id"]`(外部指定)を無条件に採用する
        # ため、呼び出し元が同一run_idを重複して渡すと`self._runs`のキーが衝突し、
        # 既存runの状態を無言で上書きしてしまう。一意性は呼び出し元の契約
        # (`_resolve_run_id`のdocstring参照)だが、違反時はサイレントな上書きより
        # fail-fastの方が安全なため、ここで検出して例外にする。自己採番
        # (`uuid.uuid4()`)側は衝突が天文学的に起こらないため対象外で構わない。
        if run_id in self._runs:
            raise ClaudeAgentProviderError(
                f"run_id {run_id!r} is already in use; McpServerSpec.config['run_id'] "
                "must be unique per run (the caller is responsible for uniqueness)"
            )
        # `mcp_servers`の構成不備(`ClaudeAgentProviderError`)はrunを
        # `self._runs`へ登録する前に検出する(モジュールdocstring参照)。
        mcp_servers = _build_mcp_servers(task, run_id)
        self._runs[run_id] = _RunState(run_id=run_id, task=task, mcp_servers=mcp_servers)
        return AgentRunHandle(run_id=run_id)

    async def stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)

        if run.events is not None:
            for event in run.events:
                yield event
            return

        events: list[AgentEvent] = []
        seq = 0

        def _next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq - 1

        if run.cancel_requested:
            event = AgentEvent(
                run_id=run_id, seq=_next_seq(), kind="cancelled", payload={"message": "cancelled"}
            )
            events.append(event)
            run.status = "cancelled"
            run.events = events
            yield event
            return

        options = _build_options(run.task, run_id, run.mcp_servers)
        tool_names: dict[str, str] = {}
        deadline = time.monotonic() + run.task.timeout_sec

        try:
            async for event in self._drive(run, options, events, _next_seq, tool_names, deadline):
                yield event
        except Exception as exc:
            # `_drive`が`ClaudeSDKError`以外の予期しない例外で中断した場合の
            # 最終防波堤(Gate2レビュー指摘): これが無いとrunが終端イベント無しで
            # status="running"のまま固定され、result()も"running"を返し続け、
            # 以後のstream()呼び出しも(`run.events`がNoneのままなので)毎回
            # `_drive`を再実行してしまう。`asyncio.CancelledError`/`GeneratorExit`
            # は`BaseException`でありここでは意図的に捕捉しない(非同期
            # ジェネレータの協調的キャンセル/`aclose()`プロトコルを素通しする
            # 必要があるため、業務上の失敗として扱うとその意味論を壊す)。
            event = self._terminal_event(run, _next_seq(), status="failed", error=str(exc))
            events.append(event)
            run.events = events
            yield event
        else:
            run.events = events

    async def _drive(
        self,
        run: _RunState,
        options: ClaudeAgentOptions,
        events: list[AgentEvent],
        next_seq: Callable[[], int],
        tool_names: dict[str, str],
        deadline: float,
    ) -> AsyncIterator[AgentEvent]:
        attempt = 0
        received_any_message = False

        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = _resolve_interrupted_status(run, default="truncated")
                event = self._terminal_event(run, next_seq(), status=status, error=None)
                events.append(event)
                yield event
                return

            client = ClaudeSDKClient(options)
            try:
                # `connect()`自体(CLIサブプロセスのハンドシェイク)も
                # `timeout_sec`の対象に含める(Gate2レビュー指摘): 以前は
                # メッセージ受信の`wait_for`にしか掛かっておらず、CLI起動が
                # ハングするとrunが`timeout_sec`を無視して無期限に
                # `running`のままになっていた。
                try:
                    await asyncio.wait_for(
                        client.connect(run.task.prompt), timeout=max(remaining, 0.01)
                    )
                except TimeoutError:
                    status = _resolve_interrupted_status(run, default="truncated")
                    event = self._terminal_event(run, next_seq(), status=status, error=None)
                    events.append(event)
                    yield event
                    return
                run.client = client

                messages = client.receive_response()
                while True:
                    if run.cancel_requested:
                        with contextlib.suppress(Exception):
                            await client.interrupt()

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = _resolve_interrupted_status(run, default="truncated")
                        event = self._terminal_event(run, next_seq(), status=status, error=None)
                        events.append(event)
                        yield event
                        return

                    try:
                        message = await asyncio.wait_for(
                            messages.__anext__(), timeout=max(remaining, 0.01)
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        status = _resolve_interrupted_status(run, default="truncated")
                        event = self._terminal_event(run, next_seq(), status=status, error=None)
                        events.append(event)
                        yield event
                        return

                    received_any_message = True

                    if isinstance(message, ResultMessage):
                        status, error = _map_terminal_status(
                            message,
                            cancel_requested=run.cancel_requested,
                            budget_exceeded=run.budget_exceeded,
                        )
                        run.turns = message.num_turns
                        run.usage = _token_usage_from_dict(message.usage)
                        event = self._terminal_event(run, next_seq(), status=status, error=error)
                        events.append(event)
                        yield event
                        return

                    for out_event in self._normalize(run, message, next_seq, tool_names):
                        events.append(out_event)
                        yield out_event

                    if run.cancel_requested:
                        continue

                    latest_usage = getattr(message, "usage", None)
                    if isinstance(latest_usage, dict):
                        total = int(latest_usage.get("input_tokens") or 0) + int(
                            latest_usage.get("output_tokens") or 0
                        )
                        if exceeds_token_budget(total, run.task.max_tokens_budget):
                            run.budget_exceeded = True
                            with contextlib.suppress(Exception):
                                await client.interrupt()

                # StopAsyncIteration without a ResultMessage: `interrupt()`だけでは
                # 必ずしもResultMessageを生まないため、`_resolve_interrupted_status`
                # 経由でcancel/予算超過の状態を確認してから既定の"completed"に
                # フォールバックする(Gate2レビュー指摘: 以前は無条件にcompletedと
                # しており、キャンセル済み/truncated済みのrunがここで状態を
                # 巻き戻されていた)。
                fallback_status = _resolve_interrupted_status(run, default="completed")
                event = self._terminal_event(run, next_seq(), status=fallback_status, error=None)
                events.append(event)
                yield event
                return
            except ClaudeSDKError as exc:
                if received_any_message or isinstance(exc, CLINotFoundError):
                    event = self._terminal_event(run, next_seq(), status="failed", error=str(exc))
                    events.append(event)
                    yield event
                    return
                if run.cancel_requested:
                    # 接続確立前(`run.client`未設定)に`cancel()`が呼ばれた場合、
                    # `interrupt()`を送る先が無く実際には接続失敗まで中断できない
                    # (Gate2レビュー指摘)。接続が(タイムアウトではなく)エラーで
                    # 終わった時点で`cancel_requested`を見て、リトライせず
                    # `cancelled`として終了する。`budget_exceeded`は
                    # `received_any_message`が真の場合のみ立つため、ここでは
                    # チェック不要(このガードは`received_any_message`が偽の
                    # 分岐でのみ到達する)。
                    event = self._terminal_event(run, next_seq(), status="cancelled", error=None)
                    events.append(event)
                    yield event
                    return
                # バックオフ後の再試行がdeadlineを超過しないことを確認する
                # (Gate2レビュー指摘): 以前はbackoffの秒数がdeadlineを
                # 考慮しておらず、timeout_secが小さい場合に合計待機が
                # timeout_secを超過しうった。残り時間切れなら試行回数の
                # 上限未到達でもリトライせず"truncated"で終了する。
                remaining = deadline - time.monotonic()
                if attempt >= _MAX_CONNECT_ATTEMPTS or remaining <= 0:
                    if remaining <= 0:
                        event = self._terminal_event(
                            run, next_seq(), status="truncated", error=None
                        )
                    else:
                        event = self._terminal_event(
                            run, next_seq(), status="failed", error=str(exc)
                        )
                    events.append(event)
                    yield event
                    return
                await asyncio.sleep(min(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), remaining))
                continue
            finally:
                run.client = None
                with contextlib.suppress(Exception):
                    await client.disconnect()

    def _terminal_event(
        self, run: _RunState, seq: int, *, status: AgentRunStatus, error: str | None
    ) -> AgentEvent:
        run.status = status
        run.error = error
        status_to_kind: dict[AgentRunStatus, AgentEventKind] = {
            "completed": "done",
            "truncated": "done",
            "failed": "error",
            "cancelled": "cancelled",
        }
        kind = status_to_kind[status]
        payload: dict[str, Any] = {"status": status, "staged_ops_count": run.staged_ops_count}
        if error is not None:
            payload["error"] = error
        return AgentEvent(run_id=run.run_id, seq=seq, kind=kind, payload=payload, usage=run.usage)

    def _normalize(
        self,
        run: _RunState,
        message: AssistantMessage | UserMessage | Any,
        next_seq: Callable[[], int],
        tool_names: dict[str, str],
    ) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="text",
                            payload={"text": block.text},
                        )
                    )
                elif isinstance(block, ThinkingBlock):
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="thinking",
                            payload={"text": block.thinking},
                        )
                    )
                elif isinstance(block, ToolUseBlock):
                    tool_names[block.id] = block.name
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="tool_use",
                            tool_name=block.name,
                            payload={"input": block.input, "tool_use_id": block.id},
                        )
                    )
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    tool_name = tool_names.get(block.tool_use_id)
                    output = _extract_tool_result_payload(block.content)
                    if (
                        tool_name == _SCORE_APPLY_OPS_TOOL_NAME
                        and isinstance(output, dict)
                        and output.get("ok") is True
                    ):
                        run.staged_ops_count += 1
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="tool_result",
                            tool_name=tool_name,
                            payload={"output": output, "is_error": block.is_error},
                        )
                    )
        return out

    async def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        run.cancel_requested = True
        if run.status == "running":
            run.status = "cancelled"
        if run.client is not None:
            # ベストエフォート: 失敗してもcancel自体は成功扱いにする。
            with contextlib.suppress(Exception):
                await run.client.interrupt()

    async def result(self, run_id: str) -> AgentResult:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        return AgentResult(
            run_id=run_id,
            status=run.status,
            turns=run.turns,
            usage=run.usage,
            staged_ops_count=run.staged_ops_count,
            error=run.error,
        )
