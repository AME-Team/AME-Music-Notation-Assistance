"""L2 Coding Agentランタイム: プロバイダ抽象とAgentEvent正規化(#42, 設計書§8.2)。

**プロバイダ固有のSDK型はこのモジュールおよび`agent/providers/`配下の各アダプタ
実装にのみ現れる**(NFR-16)。Claude Agent SDK(#48)・OpenCode SDK(#52)の
APIが将来変わっても、影響は`agent/providers/`の該当ファイルに閉じ込められる
(R-12)。

`AgentEvent`の正規化がこの抽象の要(設計書§8.2): 呼び出し元(将来のAgent Run
API、#49、およびAgentConsole、#51)はこの`AgentEvent`/`AgentResult`のみを見て、
どちらのプロバイダが動いているかを意識しない。

`domain/`/`pipeline/`は本モジュールにも依存しない(L2はL0/L1のDSPパイプライン
とは独立したM5固有の層)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

AgentEventKind = Literal["thinking", "text", "tool_use", "tool_result", "error", "done"]
AgentRunStatus = Literal["running", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class TokenUsage:
    """トークン使用量。L1の`usage`辞書(#39/#104)と意図的に同じキー構成にし、

    フロントの表示コンポーネントを再利用しやすくする。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class McpServerSpec:
    """MCPサーバ1つ分の起動仕様(設計書§8.2の`AgentTask.mcp_servers`)。

    score-mcp本体(tools.py)は#43、2種のアダプタ(インプロセス/stdio)は#44の
    担当。本Issue(#42)の時点では`AgentTask`が受け取れる形だけを定義する。
    """

    name: str
    kind: Literal["in_process", "stdio"]
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTask:
    """1回のエージェント実行の入力(設計書§8.2)。"""

    task_type: str
    project_id: str
    prompt: str
    workspace: Path
    mcp_servers: list[McpServerSpec] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    max_turns: int = 40
    max_tokens_budget: int = 300_000
    timeout_sec: int = 900


@dataclass(frozen=True)
class AgentRunHandle:
    """`AgentProvider.start()`の戻り値。呼び出し元はこの`run_id`だけを保持し、

    以降`stream`/`cancel`/`result`へ渡す。
    """

    run_id: str


@dataclass(frozen=True)
class AgentEvent:
    """両プロバイダのイベントを正規化した共通形(設計書§8.2/§11.4)。

    UIへはこの形のみが流れる。`kind`に応じて`payload`の中身が変わる
    (例: `tool_use`/`tool_result`では`tool_name`も併せて埋める)。
    """

    run_id: str
    seq: int
    kind: AgentEventKind
    payload: dict[str, Any] = field(default_factory=dict)
    tool_name: str | None = None
    usage: TokenUsage | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class AgentResult:
    """`AgentProvider.result()`の戻り値。設計書§11.3の

    `GET /api/agent/runs/{run_id}` レスポンス `{ status, turns, usage,
    staged_ops_count }` に対応する。
    """

    run_id: str
    status: AgentRunStatus
    turns: int
    usage: TokenUsage
    staged_ops_count: int = 0
    error: str | None = None


@runtime_checkable
class AgentProvider(Protocol):
    """L2エージェントプロバイダの抽象(設計書§8.2)。SDK固有の型は一切現れない。

    `agent/providers/`配下の各アダプタ(#48のClaudeAgentProvider、#52の
    OpenCodeProvider、本Issューの`DummyAgentProvider`)がこれを実装する。
    `stream`は`async def`かつ内部で`yield`する非同期ジェネレータとして実装する
    (呼び出し元は`await`せず`async for event in provider.stream(run_id):`で
    直接イテレートする)。この呼び出し形に合わせ、Protocol側の`stream`は
    (`async def`ではなく)戻り値`AsyncIterator[AgentEvent]`を返す通常の`def`
    として宣言する — `async def stream(...) -> AsyncIterator[...]: ...`と
    書くと型チェッカーは「`AsyncIterator`を返すコルーチン」(呼び出しに
    `await`が要る)と解釈してしまい、実際の非同期ジェネレータ実装(呼び出しが
    同期的で戻り値を直接`async for`できる)と食い違う。
    """

    name: str

    async def start(self, task: AgentTask) -> AgentRunHandle: ...

    def stream(self, run_id: str) -> AsyncIterator[AgentEvent]: ...

    async def cancel(self, run_id: str) -> None: ...

    async def result(self, run_id: str) -> AgentResult: ...
