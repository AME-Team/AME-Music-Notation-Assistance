"""#49: Agent Run API(POST .../agent/runs, GET .../runs/{id}, SSE events, audit, tasks)の結合テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from app.config import Settings
from app.domain.score import Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import db, storage
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


async def _collect_agent_sse_events(
    client: httpx.AsyncClient, run_id: str
) -> list[dict]:
    events: list[dict] = []
    async with client.stream("GET", f"/api/agent/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
            if events and events[-1].get("kind") in ("done", "error", "cancelled"):
                break
    return events


async def test_create_run_and_stream_events_via_sse(
    async_client: httpx.AsyncClient, settings: Settings
) -> None:
    _create_project(settings.workspace_dir, "proj_1")

    resp = await async_client.post(
        "/api/projects/proj_1/agent/runs",
        json={"task_type": "refine-part", "provider": "dummy"},
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]

    events = await _collect_agent_sse_events(async_client, run_id)
    assert [e["kind"] for e in events] == [
        "thinking",
        "tool_use",
        "tool_result",
        "done",
    ]
    assert all(e["run_id"] == run_id for e in events)
    assert events[-1]["usage"]["input_tokens"] == 120

    resp = await async_client.get(f"/api/agent/runs/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["turns"] == 1
    assert data["staged_ops_count"] == 0
    assert data["usage"]["output_tokens"] == 40


async def test_create_run_rejects_unknown_task_type(
    async_client: httpx.AsyncClient, settings: Settings
) -> None:
    _create_project(settings.workspace_dir, "proj_1")
    resp = await async_client.post(
        "/api/projects/proj_1/agent/runs",
        json={"task_type": "nope", "provider": "dummy"},
    )
    assert resp.status_code == 422


async def test_create_run_investigate_requires_prompt(
    async_client: httpx.AsyncClient, settings: Settings
) -> None:
    _create_project(settings.workspace_dir, "proj_1")
    resp = await async_client.post(
        "/api/projects/proj_1/agent/runs",
        json={"task_type": "investigate", "provider": "dummy"},
    )
    assert resp.status_code == 422


async def test_create_run_for_nonexistent_project_404(
    async_client: httpx.AsyncClient,
) -> None:
    resp = await async_client.post(
        "/api/projects/nonexistent/agent/runs",
        json={"task_type": "refine-part", "provider": "dummy"},
    )
    assert resp.status_code == 404


async def test_get_run_status_404_for_unknown_run(
    async_client: httpx.AsyncClient,
) -> None:
    resp = await async_client.get("/api/agent/runs/unknown_run")
    assert resp.status_code == 404


async def test_get_events_404_for_unknown_run(async_client: httpx.AsyncClient) -> None:
    resp = await async_client.get("/api/agent/runs/unknown_run/events")
    assert resp.status_code == 404


async def test_get_audit_log_empty_for_dummy_run(
    async_client: httpx.AsyncClient, settings: Settings
) -> None:
    _create_project(settings.workspace_dir, "proj_1")
    resp = await async_client.post(
        "/api/projects/proj_1/agent/runs",
        json={"task_type": "refine-part", "provider": "dummy"},
    )
    run_id = resp.json()["run_id"]
    await _collect_agent_sse_events(async_client, run_id)

    resp = await async_client.get(f"/api/agent/runs/{run_id}/audit")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_cancel_already_completed_run_via_api_returns_400(
    async_client: httpx.AsyncClient, settings: Settings
) -> None:
    _create_project(settings.workspace_dir, "proj_1")
    resp = await async_client.post(
        "/api/projects/proj_1/agent/runs",
        json={"task_type": "refine-part", "provider": "dummy"},
    )
    run_id = resp.json()["run_id"]
    await _collect_agent_sse_events(async_client, run_id)  # 完了まで待つ

    resp = await async_client.post(f"/api/agent/runs/{run_id}/cancel")
    assert resp.status_code == 400


async def test_list_task_definitions(async_client: httpx.AsyncClient) -> None:
    resp = await async_client.get("/api/agent/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    ids = {t["id"] for t in tasks}
    assert ids == {
        "refine-part",
        "consistency-pass",
        "repeat-alignment",
        "ghost-sweep",
        "voicing-fix",
        "export-qa",
        "investigate",
    }
