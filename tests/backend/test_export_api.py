"""#27: エクスポートAPI(`POST /api/projects/{id}/export`)のテスト。

`pipeline/export`自体のレンダリング精度は`test_export_musicxml.py`で検証済み。
ここではAPI層の配線(404/422/保存先/レスポンス)のみを検証する。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo, Spelling
from app.infra import storage
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


def test_export_404_before_quantize(client: TestClient, tiny_wav_bytes: bytes) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(
        f"/api/projects/{project_id}/export", json={"format": "musicxml"}
    )
    assert resp.status_code == 404


def test_export_404_for_missing_project(client: TestClient) -> None:
    resp = client.post("/api/projects/nonexistent/export", json={"format": "musicxml"})
    assert resp.status_code == 404


def test_export_422_for_unquantized_score(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """量子化(onset_tick等)未完了のScore IRはExportErrorとして422になる。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=False)

    resp = client.post(
        f"/api/projects/{project_id}/export", json={"format": "musicxml"}
    )
    assert resp.status_code == 422, resp.text


def test_export_422_for_unquantized_score_midi_format(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#27-M2レビュー指摘): musicxml/midi両方とも`build_score`のバリデーション

    (`score_builder.ExportError`)を共有しているため、MIDI側でも同じ422に
    なることをテストで固定化する(片方だけ確認していると将来の実装分岐に
    気づけない)。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=False)

    resp = client.post(f"/api/projects/{project_id}/export", json={"format": "midi"})
    assert resp.status_code == 422, resp.text


def test_export_musicxml_returns_valid_document_and_persists_it(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=True)

    resp = client.post(
        f"/api/projects/{project_id}/export", json={"format": "musicxml"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/vnd.recordare.musicxml+xml"
    ET.fromstring(resp.content)  # 有効なXMLとしてパースできる

    saved = storage.musicxml_export_path(settings.workspace_dir, project_id)
    assert saved.exists()
    assert saved.read_bytes() == resp.content


def test_export_midi_returns_smf_and_persists_it(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=True)

    resp = client.post(f"/api/projects/{project_id}/export", json={"format": "midi"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/midi"
    assert resp.content[:4] == b"MThd"  # Standard MIDI Fileのマジックナンバー

    saved = storage.midi_export_path(settings.workspace_dir, project_id)
    assert saved.exists()
    assert saved.read_bytes() == resp.content


def test_export_rejects_unknown_format(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=True)

    resp = client.post(f"/api/projects/{project_id}/export", json={"format": "wav"})
    assert resp.status_code == 422


def test_export_rejects_unimplemented_options_field(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#27-M2レビュー指摘): `options`は`pipeline/export`側が未対応のため、

    黙って無視せず422で拒否するべき(`ExportRequest`の`extra="forbid"`)。
    `parts`は#36で実装したため、この回帰テストの対象は`options`のみに絞る。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, quantized=True)

    resp = client.post(
        f"/api/projects/{project_id}/export",
        json={"format": "musicxml", "options": {}},
    )
    assert resp.status_code == 422, resp.text


def _write_score_with_two_parts(settings: Settings, project_id: str) -> None:
    """#36: パート選択(`parts`フィルタ)テスト用に2パート(quantized済み)の

    Score IRを書く。
    """
    service = ScoreService(workspace_dir=settings.workspace_dir)
    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="song.wav", duration_sec=2.0, sample_rate=8000),
    )
    for part_id, name, midi in (("piano", "Piano", 60), ("bass", "Bass", 40)):
        part = Part(
            id=part_id,
            name=name,
            midi_program=0,
            staves=1,
            clefs=[Clef(staff=1, sign="G", line=2)],
        )
        part.notes.append(
            Note(
                id=score.allocate_note_id(),
                onset_sec=0.0,
                duration_sec=0.5,
                onset_tick=0,
                duration_tick=480,
                midi=midi,
                velocity=90,
                provenance="amt",
                spelling=Spelling(step="C", alter=0, octave=4),
                voice=1,
                staff=1,
            )
        )
        score.parts.append(part)
    service.write_score(project_id, score)


def test_export_with_parts_filter_includes_only_selected_parts(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#36: `parts`を指定すると、指定したpart idのみがエクスポートされる。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_with_two_parts(settings, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/export",
        json={"format": "musicxml", "parts": ["piano"]},
    )
    assert resp.status_code == 200, resp.text
    root = ET.fromstring(resp.content)
    part_ids = [el.get("id") for el in root.findall(".//score-part")]
    assert part_ids == ["piano"]


def test_export_rejects_unknown_part_id(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#36: 存在しないpart idを指定した場合は422(不正なリクエストとして拒否)。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_with_two_parts(settings, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/export",
        json={"format": "musicxml", "parts": ["nonexistent"]},
    )
    assert resp.status_code == 422, resp.text
