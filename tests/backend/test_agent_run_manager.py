"""#49: AgentRunManager の単体テスト。DummyAgentProviderのみを使い、外部CLIに依存しない。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.agent.provider import (
    AgentEvent,
    AgentResult,
    AgentRunHandle,
    AgentRunNotFoundError,
    AgentTask,
    TokenUsage,
)
from app.agent.providers.dummy import DummyAgentProvider
from app.domain.score import Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import db, storage
from app.services.agent_run_manager import (
    AgentRunManager,
    MissingPromptError,
    UnknownProviderError,
    UnknownTaskTypeError,
)
from app.services.agent_run_service import AgentRunService
from app.services.project_service import ProjectNotFoundError
from app.services.score_service import ScoreService


def _create_project(workspace_dir: Path, project_id: str) -> None:
    storage.ensure_project_layout(workspace_dir, project_id)
    conn = db.get_connection(workspace_dir / "db.sqlite3")
    conn.execute(
        "INSERT INTO projects(id, name, original_filename, audio_format, created_at) "
        "VALUES (?, 'Test', 'song.wav', 'wav', '2026-01-01T00:00:00Z')",
        (project_id,),
    )
    conn.commit()
    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="song.wav", duration_sec=10.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[Part(id="piano", name="Piano", midi_program=0, staves=1)],
    )
    ScoreService(workspace_dir).write_score(project_id, score)


def _manager(workspace_dir: Path) -> AgentRunManager:
    return AgentRunManager(
        workspace_dir=workspace_dir,
        agent_run_service=AgentRunService(workspace_dir=workspace_dir),
        providers={"dummy": DummyAgentProvider()},
    )


async def _drain(manager: AgentRunManager, run_id: str) -> list:
    queue = manager.subscribe(run_id)
    events = []
    while True:
        event = await queue.get()
        if event is None:
            break
        events.append(event)
    return events


async def test_create_run_drives_dummy_provider_to_completion(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)

    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="dummy"
    )
    events = await _drain(manager, run_id)

    assert [e.kind for e in events] == ["thinking", "tool_use", "tool_result", "done"]
    assert all(e.run_id == run_id for e in events)

    run = manager.agent_run_service.get_run(run_id)
    assert run["status"] == "completed"
    assert run["turns"] == 1
    assert run["usage"] == {
        "input_tokens": 120,
        "output_tokens": 40,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assert run["staged_ops_count"] == 0


async def test_late_subscriber_replays_full_event_history(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="dummy"
    )
    await _drain(manager, run_id)

    replayed = await _drain(manager, run_id)
    assert [e.kind for e in replayed] == ["thinking", "tool_use", "tool_result", "done"]


async def test_create_run_rejects_unknown_task_type(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)
    with pytest.raises(UnknownTaskTypeError):
        await manager.create_run(
            project_id="proj_1", task_type="not-a-real-task", provider_name="dummy"
        )


async def test_create_run_rejects_unknown_provider(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)
    with pytest.raises(UnknownProviderError):
        await manager.create_run(
            project_id="proj_1",
            task_type="refine-part",
            provider_name="not-a-real-provider",
        )


async def test_create_run_requires_prompt_for_investigate(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)
    with pytest.raises(MissingPromptError):
        await manager.create_run(
            project_id="proj_1", task_type="investigate", provider_name="dummy"
        )


async def test_create_run_propagates_project_not_found(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ProjectNotFoundError):
        await manager.create_run(
            project_id="nonexistent", task_type="refine-part", provider_name="dummy"
        )


async def test_subscribe_unknown_run_raises_not_found(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(AgentRunNotFoundError):
        manager.subscribe("unknown_run")


async def test_cancel_before_drive_run_starts_produces_no_provider_events(
    tmp_path: Path,
) -> None:
    """`create_run()`にはawaitが無いため、戻った時点でバックグラウンドタスクは

    まだ1行も実行されていない。この直後にcancel_run()を呼べば、
    `_drive_run`は`provider.start()`すら呼ばず即座にcancelledで終了する
    (JobManager._run_jobと同じレース対策)。
    """
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="dummy"
    )

    result = await manager.cancel_run(run_id)
    assert result["status"] == "cancelled"

    # バックグラウンドタスクに実行機会を与える。
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    run = manager.agent_run_service.get_run(run_id)
    assert run["status"] == "cancelled"


async def test_cancel_after_completion_raises_value_error(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    manager = _manager(tmp_path)
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="dummy"
    )
    await _drain(manager, run_id)

    with pytest.raises(ValueError, match="already completed"):
        await manager.cancel_run(run_id)


class _BrokenStreamProvider:
    """`stream()`が(接続確立後に)予期しない例外を送出するフェイクプロバイダ。

    Gate2レビュー指摘(MIDDLE)の回帰テスト用: 以前は`_drive_run`がこの例外を
    捕捉しておらず、runが`running`のまま固定され`_close_subscribers`も
    呼ばれずSSE購読者がハングしていた。
    """

    name = "broken"

    async def start(self, task: AgentTask) -> AgentRunHandle:
        return AgentRunHandle(run_id="internal-broken")

    async def stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        raise RuntimeError("boom: unexpected provider bug")
        yield  # pragma: no cover - unreachable, keeps this an async generator function

    async def cancel(self, run_id: str) -> None:
        pass

    async def result(self, run_id: str) -> AgentResult:
        raise AssertionError(
            "result() should not be called since stream() raised first"
        )


async def test_provider_stream_exception_marks_run_failed_and_closes_subscribers(
    tmp_path: Path,
) -> None:
    _create_project(tmp_path, "proj_1")
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"broken": _BrokenStreamProvider()},
    )
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="broken"
    )

    # subscribe()はまだバックグラウンドタスクが走っていない時点で呼ぶため、
    # ライブqueueに登録される。ここでNoneが届く(=ハングしない)ことを確認する。
    queue = manager.subscribe(run_id)
    event = await asyncio.wait_for(queue.get(), timeout=5.0)
    assert event is None

    run = manager.agent_run_service.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"] is not None
    assert "boom" in run["error"]


class _PausableProvider:
    """`resume`イベントがセットされるまで2件目を保留するフェイクプロバイダ。

    Gate2レビュー指摘(MIDDLE)の回帰テスト用: 実行中のrunに対する`subscribe()`が
    既に発行済みのイベント(1件目)を取りこぼさないことを確認するために使う。
    """

    name = "pausable"

    def __init__(self) -> None:
        self.resume = asyncio.Event()

    async def start(self, task: AgentTask) -> AgentRunHandle:
        return AgentRunHandle(run_id="internal-pausable")

    async def stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            run_id=run_id, seq=0, kind="thinking", payload={"text": "step1"}
        )
        await self.resume.wait()
        yield AgentEvent(run_id=run_id, seq=1, kind="done", payload={})

    async def cancel(self, run_id: str) -> None:
        pass

    async def result(self, run_id: str) -> AgentResult:
        return AgentResult(
            run_id=run_id, status="completed", turns=1, usage=TokenUsage()
        )


async def test_subscribe_mid_run_receives_earlier_events_via_history_replay(
    tmp_path: Path,
) -> None:
    _create_project(tmp_path, "proj_1")
    provider = _PausableProvider()
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"pausable": provider},
    )
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="pausable"
    )

    # 最初のイベント(thinking)が発行されるまでバックグラウンドタスクを進める。
    while not manager._event_history.get(run_id):
        await asyncio.sleep(0)

    # 実行中(2件目はまだ)のこの時点で購読する。
    run = manager.agent_run_service.get_run(run_id)
    assert run["status"] == "running"
    queue = manager.subscribe(run_id)

    first = await queue.get()
    assert first.kind == "thinking"

    provider.resume.set()
    second = await queue.get()
    assert second.kind == "done"


async def test_cancel_during_stream_is_not_overwritten_by_late_completion(
    tmp_path: Path,
) -> None:
    """Gate2レビュー指摘(MIDDLE)の回帰テスト: provider.cancel()は

    `contextlib.suppress`で包まれたベストエフォートであり、実際には
    中断できないprovider(このテストの`_PausableProvider.cancel()`はno-op)
    では、streamが自然に最後まで進んで`result()`がstatus="completed"を
    返しても、既に`cancel_run()`が確定させた"cancelled"を上書きしては
    ならないことを確認する。
    """
    _create_project(tmp_path, "proj_1")
    provider = _PausableProvider()
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"pausable": provider},
    )
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="pausable"
    )

    while not manager._event_history.get(run_id):
        await asyncio.sleep(0)

    await manager.cancel_run(run_id)
    run = manager.agent_run_service.get_run(run_id)
    assert run["status"] == "cancelled"

    # providerはcancelを無視してstreamを最後まで進める。
    provider.resume.set()
    for _ in range(50):
        await asyncio.sleep(0)

    run_after = manager.agent_run_service.get_run(run_id)
    assert run_after["status"] == "cancelled"


class _RecordingProvider:
    """`provider.start()`に渡された`AgentTask`をそのまま記録するフェイクプロバイダ。

    #50の回帰テスト用: consistency-pass/voicing-fixが実際に専用プロンプト/
    予算(max_tokens_budget/timeout_sec)を`AgentTask`へ渡していることを検証する。
    """

    name = "recording"

    def __init__(self) -> None:
        self.received_task: AgentTask | None = None

    async def start(self, task: AgentTask) -> AgentRunHandle:
        self.received_task = task
        return AgentRunHandle(run_id="internal-recording")

    async def stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(run_id=run_id, seq=0, kind="done", payload={})

    async def cancel(self, run_id: str) -> None:
        pass

    async def result(self, run_id: str) -> AgentResult:
        return AgentResult(
            run_id=run_id, status="completed", turns=1, usage=TokenUsage()
        )


async def test_consistency_pass_uses_task_specific_prompt_and_budget(
    tmp_path: Path,
) -> None:
    _create_project(tmp_path, "proj_1")
    provider = _RecordingProvider()
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"recording": provider},
    )
    run_id = await manager.create_run(
        project_id="proj_1", task_type="consistency-pass", provider_name="recording"
    )
    await _drain(manager, run_id)

    assert provider.received_task is not None
    assert "consistency-pass専用の作業手順" in provider.received_task.prompt
    assert "mcp__score__score_validate" in provider.received_task.prompt
    assert provider.received_task.max_turns == 40
    assert provider.received_task.max_tokens_budget == 400_000
    assert provider.received_task.timeout_sec == 1200


async def test_voicing_fix_embeds_scope_in_prompt_and_uses_task_budget(
    tmp_path: Path,
) -> None:
    _create_project(tmp_path, "proj_1")
    provider = _RecordingProvider()
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"recording": provider},
    )
    run_id = await manager.create_run(
        project_id="proj_1",
        task_type="voicing-fix",
        provider_name="recording",
        scope={"part_id": "piano", "bars": [10, 20]},
    )
    await _drain(manager, run_id)

    assert provider.received_task is not None
    assert "voicing-fix専用の作業手順" in provider.received_task.prompt
    assert "piano" in provider.received_task.prompt
    assert provider.received_task.max_turns == 20
    assert provider.received_task.max_tokens_budget == 200_000
    assert provider.received_task.timeout_sec == 600


async def test_generic_task_falls_back_to_agent_task_defaults(tmp_path: Path) -> None:
    """`prompt_template`/`max_tokens_budget`/`timeout_sec`が未設定のタスク

    (#50時点ではconsistency-pass/voicing-fix以外)は、`AgentTask`自身の既定値
    (max_tokens_budget=300,000、timeout_sec=900)を使うことを確認する。
    """
    _create_project(tmp_path, "proj_1")
    provider = _RecordingProvider()
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"recording": provider},
    )
    run_id = await manager.create_run(
        project_id="proj_1", task_type="refine-part", provider_name="recording"
    )
    await _drain(manager, run_id)

    assert provider.received_task is not None
    assert "専用の作業手順" not in provider.received_task.prompt
    assert provider.received_task.max_tokens_budget == 300_000
    assert provider.received_task.timeout_sec == 900


async def test_explicit_budget_overrides_task_default(tmp_path: Path) -> None:
    _create_project(tmp_path, "proj_1")
    provider = _RecordingProvider()
    manager = AgentRunManager(
        workspace_dir=tmp_path,
        agent_run_service=AgentRunService(workspace_dir=tmp_path),
        providers={"recording": provider},
    )
    run_id = await manager.create_run(
        project_id="proj_1",
        task_type="consistency-pass",
        provider_name="recording",
        budget=123_456,
    )
    await _drain(manager, run_id)

    assert provider.received_task is not None
    assert provider.received_task.max_tokens_budget == 123_456
