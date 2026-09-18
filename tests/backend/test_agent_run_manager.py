"""#49: AgentRunManager の単体テスト。DummyAgentProviderのみを使い、外部CLIに依存しない。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.provider import AgentRunNotFoundError
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
