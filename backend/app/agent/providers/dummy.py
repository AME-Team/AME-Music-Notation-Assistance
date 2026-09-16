"""`AgentProvider`のダミー実装(#42完了条件: 「ダミープロバイダで一連が動作する」)。

外部プロセス・外部SDKには一切依存せず、固定4イベント(`thinking`→`tool_use`
→`tool_result`→`done`)を返す。#48/#52の実プロバイダ実装前に、上位層
(将来のAgent Run API #49、AgentConsole #51)と契約テスト(`agent_contract.py`)
の両方を検証可能にすることが目的。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Final

from app.agent.provider import (
    AgentEvent,
    AgentEventKind,
    AgentResult,
    AgentRunHandle,
    AgentRunNotFoundError,
    AgentTask,
    TokenUsage,
)

_DUMMY_USAGE = TokenUsage(input_tokens=120, output_tokens=40)

_STEPS: Final[list[tuple[AgentEventKind, dict[str, object], str | None]]] = [
    ("thinking", {"text": "タスクを分析しています"}, None),
    ("tool_use", {"input": {}}, "score_read"),
    ("tool_result", {"output": "ok"}, "score_read"),
    ("done", {}, None),
]


class _DummyRun:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.cancelled = False
        self.status: str = "running"
        # `stream()`を複数回呼んでも(再接続・再開用途)同じイベント列/seqを
        # 再現できるよう、初回生成時にここへキャッシュする(#42 Gate2レビュー
        # 指摘・2巡目: 以前は`stream()`の呼び出しごとにイベント列を生成し直して
        # おり、2回目以降の呼び出しでseqが4,5,6,7...と継続してしまっていた)。
        self.events: list[AgentEvent] | None = None


class DummyAgentProvider:
    """`AgentProvider`Protocolの最小実装。実行状態はプロセス内メモリのみに保持する

    (永続化は将来のAgent Run API #49の担当であり、本プロバイダの責務外)。
    """

    name = "dummy"

    def __init__(self) -> None:
        self._runs: dict[str, _DummyRun] = {}

    async def start(self, task: AgentTask) -> AgentRunHandle:
        run_id = str(uuid.uuid4())
        self._runs[run_id] = _DummyRun(run_id)
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
        for seq, (kind, payload, tool_name) in enumerate(_STEPS):
            if run.cancelled:
                run.status = "cancelled"
                event = AgentEvent(
                    run_id=run_id, seq=seq, kind="cancelled", payload={"message": "cancelled"}
                )
                events.append(event)
                run.events = events
                yield event
                return
            event = AgentEvent(
                run_id=run_id,
                seq=seq,
                kind=kind,
                # `_STEPS`はモジュールロード時に一度だけ生成されるため、その
                # payload dictをそのまま使うと全run・全イベントで同一
                # オブジェクトが共有される(#42 Gate2レビュー指摘・3巡目
                # MIDDLE: 呼び出し側がevent.payloadを破壊的に更新すると、
                # 他runやキャッシュ済みrun.eventsの再生結果まで汚染される)。
                # run/イベントごとに独立したコピーを渡す。
                payload=dict(payload),
                tool_name=tool_name,
                usage=_DUMMY_USAGE if kind == "done" else None,
            )
            events.append(event)
            yield event
        run.status = "completed"
        run.events = events

    async def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        run.cancelled = True
        # `stream()`を一度も(最後まで)消費していないrunでも`result()`が直ちに
        # `cancelled`を返せるよう、ここでも遷移させる(#42 Gate2レビュー指摘・
        # 2巡目 MIDDLE: 以前は`run.cancelled`フラグを立てるだけで、`stream()`側が
        # 消費されるまで`run.status`が"running"のままだった)。完了済みrunの
        # ステータスは上書きしない。
        if run.status == "running":
            run.status = "cancelled"

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
