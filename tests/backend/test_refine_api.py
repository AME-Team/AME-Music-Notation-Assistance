"""#39: L1整音API(`POST /api/projects/{id}/refine`)のテスト。

`run_l1_sequential`自体のロジックは`test_l1_runner.py`で検証済み。ここでは
API層の配線(404/503/422/200、`current.json`が変更されないこと、
`score/staging/{run_id}.json`への保存)のみを検証する。実際のAnthropic API
呼び出しは`app.pipeline.refine.l1_runner.call_l1_chunk`をモックして防ぐ。
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo
from app.infra import storage
from app.pipeline.refine.l1_client import L1ChunkCallResult, L1ChunkResponse
from app.services.score_service import ScoreService


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _write_score(settings: Settings, project_id: str) -> None:
    service = ScoreService(workspace_dir=settings.workspace_dir)
    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="song.wav", duration_sec=2.0, sample_rate=8000),
        divisions=480,
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )
    part.notes.append(
        Note(
            id=score.allocate_note_id(),
            onset_sec=0.0,
            duration_sec=0.5,
            onset_tick=0,
            duration_tick=480,
            midi=60,
            velocity=90,
            provenance="amt",
            voice=1,
            staff=1,
        )
    )
    score.parts.append(part)
    service.write_score(project_id, score)


def _write_beatmap_120bpm_4_4(settings: Settings, project_id: str) -> None:
    beats = [
        {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
        for i in range(16)
    ]
    storage.write_json(
        storage.beatmap_path(settings.workspace_dir, project_id),
        {
            "beats": beats,
            "downbeats_sec": [b["time_sec"] for b in beats if b["beat_in_bar"] == 1],
            "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
            "tempo_map": [],
            "confidence": 1.0,
        },
    )


def _mock_l1_response():
    result = L1ChunkCallResult(
        output=L1ChunkResponse(bar_range=(1, 1), decisions=[]),
        usage={"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0},
    )
    return patch("app.pipeline.refine.l1_runner.call_l1_chunk", return_value=result)


def test_refine_404_before_transcribe(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "sync", "part_id": "piano"},
    )
    assert resp.status_code == 404


def test_refine_503_when_api_key_missing(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes, monkeypatch
) -> None:
    monkeypatch.delenv("AME_ANTHROPIC_API_KEY", raising=False)
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "sync", "part_id": "piano"},
    )
    assert resp.status_code == 503, resp.text


def test_refine_404_when_beatmap_missing(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes, monkeypatch
) -> None:
    monkeypatch.setenv("AME_ANTHROPIC_API_KEY", "dummy-key-for-test")
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    # beatmap.jsonを書かない

    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "sync", "part_id": "piano"},
    )
    assert resp.status_code == 404, resp.text


def test_refine_success_writes_staging_and_does_not_touch_current(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes, monkeypatch
) -> None:
    monkeypatch.setenv("AME_ANTHROPIC_API_KEY", "dummy-key-for-test")
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    current_path = storage.score_current_path(settings.workspace_dir, project_id)
    before = current_path.read_bytes()

    with _mock_l1_response():
        resp = client.post(
            f"/api/projects/{project_id}/refine",
            json={"mode": "sync", "part_id": "piano"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks_ok"] == 1
    assert body["chunks_rejected"] == 0

    # current.jsonは一切変更されない(#39の設計判断)。
    assert current_path.read_bytes() == before

    staging_path = storage.score_staging_path(
        settings.workspace_dir, project_id, body["run_id"]
    )
    assert staging_path.exists()


def test_refine_rejects_unknown_part_id(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes, monkeypatch
) -> None:
    monkeypatch.setenv("AME_ANTHROPIC_API_KEY", "dummy-key-for-test")
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "sync", "part_id": "nonexistent"},
    )
    assert resp.status_code == 422, resp.text


def test_refine_rejects_batch_mode(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """`mode: "batch"`は#40の担当のため422で拒否される(スキーマがLiteral["sync"]のみ許可)。"""
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "batch", "part_id": "piano"},
    )
    assert resp.status_code == 422
