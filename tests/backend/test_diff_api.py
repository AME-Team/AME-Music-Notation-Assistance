"""#41: DiffPanel API(`GET/POST /api/projects/{id}/refine/runs/{run_id}/{diff,accept,reject}`)。

`pipeline/refine/l1_diff.py`自体のロジックは`test_l1_diff.py`で検証済み。ここでは
API層の配線(404/200、`current.json`/`score/staging/{run_id}.json`への書き込み、
Undo連携、`ops.jsonl`への監査ログ追記)のみを検証する。
"""

from __future__ import annotations

from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo
from app.infra import storage
from app.services import score_undo
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient

_RUN_ID = "run_diff_test"


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _write_score_and_staging(settings: Settings, project_id: str) -> ScoreIR:
    """L0(current)に2ノート(keep対象/delete対象)を持つスコアを書き、L1(staging)提案を用意する。"""
    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="song.wav", duration_sec=4.0, sample_rate=8000),
        divisions=480,
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )
    keep_note = Note(
        id=score.allocate_note_id(),
        onset_sec=0.0,
        duration_sec=0.5,
        onset_tick=0,
        duration_tick=480,
        midi=60,
        velocity=90,
        provenance="baseline",
        voice=1,
        staff=1,
    )
    delete_note = Note(
        id=score.allocate_note_id(),
        onset_sec=0.5,
        duration_sec=0.5,
        onset_tick=480,
        duration_tick=480,
        midi=61,
        velocity=90,
        provenance="baseline",
        voice=1,
        staff=1,
    )
    part.notes.extend([keep_note, delete_note])
    score.parts.append(part)
    ScoreService(workspace_dir=settings.workspace_dir).write_score(project_id, score)

    staged = score.model_copy(deep=True)
    staged_part = staged.find_part("piano")
    assert staged_part is not None
    staged_keep, staged_delete = staged_part.notes
    staged_keep.voice = 2
    staged_keep.provenance = "llm"
    staged_keep.provenance_run_id = _RUN_ID
    staged_keep.ai_reason = "voice分離"
    staged_delete.status = "deleted"
    staged_delete.provenance = "llm"
    staged_delete.provenance_run_id = _RUN_ID
    staged_delete.ai_reason = "ゴースト候補"
    storage.write_json(
        storage.score_staging_path(settings.workspace_dir, project_id, _RUN_ID),
        staged.model_dump(mode="json"),
    )
    return score


def test_get_diff_404_for_unknown_run(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_and_staging(settings, project_id)

    resp = client.get(f"/api/projects/{project_id}/refine/runs/nonexistent/diff")
    assert resp.status_code == 404


def test_get_diff_lists_keep_and_delete_changes(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_and_staging(settings, project_id)

    resp = client.get(f"/api/projects/{project_id}/refine/runs/{_RUN_ID}/diff")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == _RUN_ID
    types = {c["change_type"] for c in body["changes"]}
    assert types == {"keep", "delete"}
    assert len(body["changes"]) == 2


def test_accept_all_updates_current_and_enables_undo(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_and_staging(settings, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/refine/runs/{_RUN_ID}/accept", json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["applied_note_ids"]) == [1, 2]
    assert body["remaining_diff"]["changes"] == []

    updated_score = ScoreService(workspace_dir=settings.workspace_dir).read_score(
        project_id
    )
    part = updated_score.find_part("piano")
    assert part is not None
    notes_by_id = {n.id: n for n in part.notes}
    assert notes_by_id[1].voice == 2
    assert notes_by_id[1].provenance_run_id == _RUN_ID
    assert notes_by_id[2].status == "deleted"

    # 再度差分取得しても既に承認済みのため現れない。
    resp = client.get(f"/api/projects/{project_id}/refine/runs/{_RUN_ID}/diff")
    assert resp.json()["changes"] == []

    # ai.acceptはUndo対象(#41着手前の合意事項): Undoボタンで取り消せる。
    undo_resp = client.post(f"/api/projects/{project_id}/score/undo")
    assert undo_resp.status_code == 200, undo_resp.text
    assert undo_resp.json()["applied"] is True
    reverted = undo_resp.json()["score"]
    reverted_notes = {n["id"]: n for n in reverted["parts"][0]["notes"]}
    assert reverted_notes[1]["voice"] == 1
    assert reverted_notes[2]["status"] == "active"

    ops_log = storage.score_ops_log_path(settings.workspace_dir, project_id)
    logged = [line for line in ops_log.read_text(encoding="utf-8").splitlines() if line]
    assert any('"ai.accept"' in line for line in logged)


def test_accept_with_bar_scope_applies_only_matching_change(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_and_staging(settings, project_id)

    # keep_note(id=1)/delete_note(id=2)はどちらもbar 1のため、
    # 存在しないbar範囲を指定すると何も適用されない。
    resp = client.post(
        f"/api/projects/{project_id}/refine/runs/{_RUN_ID}/accept",
        json={"bar_range": [5, 8]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_note_ids"] == []
    assert len(resp.json()["remaining_diff"]["changes"]) == 2


def test_reject_reverts_staging_and_leaves_current_untouched(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score_and_staging(settings, project_id)
    current_path = storage.score_current_path(settings.workspace_dir, project_id)
    before = current_path.read_bytes()

    resp = client.post(
        f"/api/projects/{project_id}/refine/runs/{_RUN_ID}/reject", json={}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["reverted_note_ids"]) == [1, 2]
    assert resp.json()["remaining_diff"]["changes"] == []

    # current.jsonは一切変更されない。
    assert current_path.read_bytes() == before

    # 却下後は再度差分取得しても現れない(stagingが書き戻されているため)。
    resp = client.get(f"/api/projects/{project_id}/refine/runs/{_RUN_ID}/diff")
    assert resp.json()["changes"] == []

    ops_log = storage.score_ops_log_path(settings.workspace_dir, project_id)
    logged = [line for line in ops_log.read_text(encoding="utf-8").splitlines() if line]
    assert any('"ai.reject"' in line for line in logged)

    # 却下はUndo/Redoスタックには積まない(currentへの変更が無いため)。
    undo_state = score_undo.read_undo_state(
        storage.score_undo_state_path(settings.workspace_dir, project_id)
    )
    assert undo_state.done == []
