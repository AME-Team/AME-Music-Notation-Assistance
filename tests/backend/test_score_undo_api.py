"""#32: `POST /score/undo`・`/score/redo`APIのテスト。

`score_undo.py`自体の細かい正常系は`test_score_undo.py`で検証済み。ここではAPI層の
配線(404/409/200、`applied`フラグ、`/score/ops`との統合)のみを検証する。
"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo
from app.infra import storage
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _write_score(settings: Settings, project_id: str) -> int:
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
    note = Note(
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
    part.notes.append(note)
    score.parts.append(part)
    service.write_score(project_id, score)
    return note.id


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


def _setup(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> tuple[str, int]:
    project_id = _create_project(client, tiny_wav_bytes)
    note_id = _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)
    return project_id, note_id


def test_undo_404_when_score_not_found(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(f"/api/projects/{project_id}/score/undo")
    assert resp.status_code == 404


def test_redo_404_when_score_not_found(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(f"/api/projects/{project_id}/score/redo")
    assert resp.status_code == 404


def test_undo_applied_false_when_stack_empty(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id, _ = _setup(client, settings, tiny_wav_bytes)
    resp = client.post(f"/api/projects/{project_id}/score/undo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False


def test_redo_applied_false_when_stack_empty(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id, _ = _setup(client, settings, tiny_wav_bytes)
    resp = client.post(f"/api/projects/{project_id}/score/redo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False


def test_edit_then_undo_restores_previous_state(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id, note_id = _setup(client, settings, tiny_wav_bytes)

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parts"][0]["notes"][0]["midi"] == 67

    resp = client.post(f"/api/projects/{project_id}/score/undo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["score"]["parts"][0]["notes"][0]["midi"] == 60


def test_undo_then_redo_reapplies(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id, note_id = _setup(client, settings, tiny_wav_bytes)
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )
    client.post(f"/api/projects/{project_id}/score/undo")

    resp = client.post(f"/api/projects/{project_id}/score/redo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["score"]["parts"][0]["notes"][0]["midi"] == 67


def test_new_edit_after_undo_clears_redo_stack(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id, note_id = _setup(client, settings, tiny_wav_bytes)
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )
    client.post(f"/api/projects/{project_id}/score/undo")
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 71}]},
    )

    resp = client.post(f"/api/projects/{project_id}/score/redo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False


def test_ops_writes_ops_jsonl_audit_log(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id, note_id = _setup(client, settings, tiny_wav_bytes)
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )
    log_path = storage.score_ops_log_path(settings.workspace_dir, project_id)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_undo_does_not_append_to_ops_jsonl(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """Undo/Redoは監査ログを一切変更しない(`score_undo.py`のdocstring参照)。"""
    project_id, note_id = _setup(client, settings, tiny_wav_bytes)
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )
    log_path = storage.score_ops_log_path(settings.workspace_dir, project_id)
    before_lines = log_path.read_text(encoding="utf-8").splitlines()

    client.post(f"/api/projects/{project_id}/score/undo")
    client.post(f"/api/projects/{project_id}/score/redo")

    after_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert after_lines == before_lines


def test_undo_concurrent_modification_returns_409(
    client: TestClient,
    settings: Settings,
    tiny_wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.score as score_module

    project_id, note_id = _setup(client, settings, tiny_wav_bytes)
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )

    score_path = storage.score_current_path(settings.workspace_dir, project_id)
    original_apply_undo = score_module.score_undo.apply_undo

    def _apply_and_concurrently_modify(score, state):
        concurrent = storage.read_json(score_path)
        concurrent["meta"]["stages"]["_concurrent_marker"] = {"touched": True}
        storage.write_json(score_path, concurrent)
        return original_apply_undo(score, state)

    monkeypatch.setattr(
        score_module.score_undo, "apply_undo", _apply_and_concurrently_modify
    )

    resp = client.post(f"/api/projects/{project_id}/score/undo")
    assert resp.status_code == 409, resp.text

    final = storage.read_json(score_path)
    assert final["meta"]["stages"].get("_concurrent_marker") == {"touched": True}
    assert final["parts"][0]["notes"][0]["midi"] == 67  # undoは反映されない


def test_undo_concurrent_undo_state_modification_returns_409(
    client: TestClient,
    settings: Settings,
    tiny_wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回帰(#32-M3レビュー指摘): score/current.jsonだけでなく

    score/undo_state.jsonも並行変更検知の対象に含める。
    """
    import app.api.score as score_module

    project_id, note_id = _setup(client, settings, tiny_wav_bytes)
    client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 67}]},
    )

    undo_state_path = storage.score_undo_state_path(settings.workspace_dir, project_id)
    original_apply_undo = score_module.score_undo.apply_undo

    def _apply_and_concurrently_modify_undo_state(score, state):
        current = storage.read_json(undo_state_path)
        current["_concurrent_marker"] = True
        storage.write_json(undo_state_path, current)
        return original_apply_undo(score, state)

    monkeypatch.setattr(
        score_module.score_undo, "apply_undo", _apply_and_concurrently_modify_undo_state
    )

    resp = client.post(f"/api/projects/{project_id}/score/undo")
    assert resp.status_code == 409, resp.text

    score = storage.read_json(
        storage.score_current_path(settings.workspace_dir, project_id)
    )
    assert score["parts"][0]["notes"][0]["midi"] == 67  # undoは反映されない
