from __future__ import annotations

import json

import httpx
from app.config import Settings
from app.infra import db


async def _create_project(client: httpx.AsyncClient, tiny_wav_bytes: bytes) -> str:
    resp = await client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _run_dummy_stage(
    client: httpx.AsyncClient, project_id: str, params: dict
) -> httpx.Response:
    return await client.post(
        f"/api/projects/{project_id}/stages/dummy/run", json={"params": params}
    )


async def _collect_sse_events(client: httpx.AsyncClient, job_id: str) -> list[dict]:
    events: list[dict] = []
    async with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
            if events and events[-1].get("status") in (
                "succeeded",
                "failed",
                "cancelled",
            ):
                break
    return events


async def test_dummy_job_progresses_to_completion_via_sse(
    async_client: httpx.AsyncClient, tiny_wav_bytes: bytes
) -> None:
    project_id = await _create_project(async_client, tiny_wav_bytes)

    resp = await _run_dummy_stage(async_client, project_id, {})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    events = await _collect_sse_events(async_client, job_id)
    assert events, "expected at least one SSE event"
    progresses = [e.get("progress", 0.0) for e in events if "progress" in e]
    assert progresses[0] <= progresses[-1]
    assert progresses[-1] == 1.0
    assert events[-1]["status"] == "succeeded"

    resp = await async_client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"
    assert resp.json()["progress"] == 1.0


async def test_unknown_stage_is_rejected(
    async_client: httpx.AsyncClient, tiny_wav_bytes: bytes
) -> None:
    project_id = await _create_project(async_client, tiny_wav_bytes)
    resp = await async_client.post(
        f"/api/projects/{project_id}/stages/not-a-real-stage/run", json={"params": {}}
    )
    assert resp.status_code == 422


async def test_job_cancel(
    async_client: httpx.AsyncClient, tiny_wav_bytes: bytes
) -> None:
    project_id = await _create_project(async_client, tiny_wav_bytes)
    resp = await _run_dummy_stage(async_client, project_id, {})
    job_id = resp.json()["job_id"]

    resp = await async_client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 204

    resp = await async_client.get(f"/api/jobs/{job_id}")
    assert resp.json()["status"] == "cancelled"


async def test_worker_crash_marks_job_failed_and_server_survives(
    async_client: httpx.AsyncClient, tiny_wav_bytes: bytes
) -> None:
    """NFR-04: Worker がクラッシュしてもジョブは failed として記録され、APIサーバは生存する。"""
    project_id = await _create_project(async_client, tiny_wav_bytes)

    resp = await _run_dummy_stage(async_client, project_id, {"crash_at": 0.4})
    job_id = resp.json()["job_id"]

    events = await _collect_sse_events(async_client, job_id)
    assert events[-1]["status"] == "failed"

    resp = await async_client.get(f"/api/jobs/{job_id}")
    assert resp.json()["status"] == "failed"
    assert resp.json()["exit_code"] != 0

    # API サーバ本体が生きていることを確認(NFR-04)。
    resp = await async_client.get("/health")
    assert resp.status_code == 200


async def test_run_stage_merges_force_flag_into_job_params(
    async_client: httpx.AsyncClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#29: `RunStageRequest.force`は`JobManager.create_job`の`params`へ

    `"force"`キーとして混ぜ込まれ、worker/dsp_main.pyの各ステージへ届く。
    """
    project_id = await _create_project(async_client, tiny_wav_bytes)
    resp = await async_client.post(
        f"/api/projects/{project_id}/stages/dummy/run",
        json={"params": {"crash_at": 0.4}, "force": True},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    row = (
        db.get_connection(settings.workspace_dir / "db.sqlite3")
        .execute("SELECT params_json FROM jobs WHERE id = ?", (job_id,))
        .fetchone()
    )
    params = json.loads(row["params_json"])
    assert params == {"crash_at": 0.4, "force": True}
