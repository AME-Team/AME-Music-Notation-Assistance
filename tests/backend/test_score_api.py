"""#23: Score IR取得API(`GET /api/projects/{id}/score`)のテスト。"""

from __future__ import annotations

from app.config import Settings
from app.domain.score import Clef, Note, Part, Spelling
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _write_quantized_score(settings: Settings, project_id: str) -> None:
    service = ScoreService(workspace_dir=settings.workspace_dir)
    from app.domain.score import ScoreIR, SourceInfo

    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="song.wav", duration_sec=2.0, sample_rate=8000),
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
            midi=60,
            velocity=90,
            provenance="amt",
            onset_tick=0,
            duration_tick=480,
            spelling=Spelling(step="C", alter=0, octave=4),
            voice=1,
            staff=1,
        )
    )
    score.parts.append(part)
    service.write_score(project_id, score)


def test_get_score_404_before_transcribe(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/score")
    assert resp.status_code == 404


def test_get_score_404_for_missing_project(client: TestClient) -> None:
    resp = client.get("/api/projects/nonexistent/score")
    assert resp.status_code == 404


def test_get_score_returns_score_ir(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_quantized_score(settings, project_id)

    resp = client.get(f"/api/projects/{project_id}/score")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == project_id
    part = next(p for p in body["parts"] if p["id"] == "piano")
    assert len(part["notes"]) == 1
    assert part["notes"][0]["spelling"] == {"step": "C", "alter": 0, "octave": 4}
