"""`AgentProvider`のダミー実装(#42完了条件: 「ダミープロバイダで一連が動作する」)。

外部プロセス・外部SDKには一切依存せず、固定4イベント(`thinking`→`tool_use`
→`tool_result`→`done`)を返す。#48/#52の実プロバイダ実装前に、上位層
(将来のAgent Run API #49、AgentConsole #51)と契約テスト(`agent_contract.py`)
の両方を検証可能にすることが目的。
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator

from app.agent.provider import (
    AgentEvent,
    AgentResult,
    AgentRunHandle,
    AgentTask,
    TokenUsage,
)

_DUMMY_USAGE = TokenUsage(input_tokens=120, output_tokens=40)


class AgentRunNotFoundError(KeyError):
    """未知の`run_id`が`stream`/`cancel`/`result`に渡された場合。"""


class _DummyRun:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.cancelled = False
        self.status: str = "running"


class DummyAgentProvider:
    """`AgentProvider`Protocolの最小実装。実行状態はプロセス内メモリのみに保持する

    (永続化は将来のAgent Run API #49の担当であり、本プロバイダの責務外)。
    """

    name = "dummy"

    def __init__(self) -> None:
        self._runs: dict[str, _DummyRun] = {}
        self._seq = itertools.count()

    async def start(self, task: AgentTask) -> AgentRunHandle:
        run_id = str(uuid.uuid4())
        self._runs[run_id] = _DummyRun(run_id)
        return AgentRunHandle(run_id=run_id)

    async def stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)

        steps: list[tuple[str, dict[str, object], str | None]] = [
            ("thinking", {"text": "タスクを分析しています"}, None),
            ("tool_use", {"input": {}}, "score_read"),
            ("tool_result", {"output": "ok"}, "score_read"),
            ("done", {}, None),
        ]
        for kind, payload, tool_name in steps:
            if run.cancelled:
                run.status = "cancelled"
                yield AgentEvent(
                    run_id=run_id,
                    seq=next(self._seq),
                    kind="error",
                    payload={"message": "cancelled"},
                )
                return
            yield AgentEvent(
                run_id=run_id,
                seq=next(self._seq),
                kind=kind,  # type: ignore[arg-type]
                payload=payload,
                tool_name=tool_name,
                usage=_DUMMY_USAGE if kind == "done" else None,
            )
        run.status = "completed"

    async def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        run.cancelled = True

    async def result(self, run_id: str) -> AgentResult:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        return AgentResult(
            run_id=run_id,
            status=run.status,  # type: ignore[arg-type]
            turns=1,
            usage=_DUMMY_USAGE,
            staged_ops_count=0,
        )
