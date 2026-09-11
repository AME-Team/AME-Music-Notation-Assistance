"""#33: 楽譜プレビューAPI(`GET /api/projects/{id}/score/preview.musicxml`)のテスト。

`pipeline/export`自体のレンダリング精度は`test_export_musicxml.py`で検証済み。
ここではAPI層の配線(404/422/200)のみを検証する。`POST /export`(#27)と
同じ`render_musicxml`を再利用するため、`test_export_api.py`と同じ構成。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo, Spelling
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _write_score(settings: Settings, project_id: str, *, quantized: bool) -> None:
    service = ScoreService(workspace_dir=settings.workspace_dir)
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
    note = Note(
        id=score.allocate_note_id(),
        onset_sec=0.0,
        duration_sec=0.5,
        midi=60,
        velocity=90,
        provenance="amt",
    )
    if quantized:
        note.onset_tick = 0
        note.duration_tick = 480
        note.spelling = Spelling(step="C", alter=0, octave=4)
        note.voice = 1
        note.staff = 1
    part.notes.append(note)
    score.parts.append(part)
    service.write_score(project_id, score)


def test_preview_404_before_transcribe(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/score/preview.musicxml")
    assert resp.status_code == 404


def test_preview_404_for_missing_project(client: TestClient) -> None:
    resp = client.get("/api/projects/nonexistent/score/preview.musicxml")
    assert resp.status_code == 404


def test_preview_422_for_unquantized_score(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=False)

    resp = client.get(f"/api/projects/{project_id}/score/preview.musicxml")
    assert resp.status_code == 422, resp.text


def test_preview_returns_valid_musicxml(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=True)

    resp = client.get(f"/api/projects/{project_id}/score/preview.musicxml")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/vnd.recordare.musicxml+xml"

    root = ET.fromstring(resp.content)
    assert root.tag == "score-partwise"


def test_preview_does_not_write_export_artifact(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#33): プレビューはPOST /exportと違い恒久成果物を書き込まない

    (都度再生成される一時プレビューのため)。
    """
    from app.infra import storage

    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=True)

    resp = client.get(f"/api/projects/{project_id}/score/preview.musicxml")
    assert resp.status_code == 200, resp.text

    out_path = storage.musicxml_export_path(settings.workspace_dir, project_id)
    assert not out_path.exists()
