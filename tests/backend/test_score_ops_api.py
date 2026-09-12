"""#31: ノート編集オペレーションAPI(`POST /api/projects/{id}/score/ops`)のテスト。

`apply_ops`自体の細かい正常系/異常系は`test_score_ops.py`で検証済み。ここでは
API層の配線(404/409/422/200、beatmapからのtick→秒変換、楽観的並行性制御)のみを
検証する。
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


def _write_score(
    settings: Settings, project_id: str, *, with_note: bool = True
) -> None:
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
    if with_note:
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


def test_ops_404_before_transcribe(client: TestClient, tiny_wav_bytes: bytes) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={
            "ops": [
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 0,
                    "duration_tick": 480,
                    "midi": 60,
                }
            ]
        },
    )
    assert resp.status_code == 404


def test_ops_404_when_beatmap_missing(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#31-M3レビュー指摘): beatmap欠落時に無言でtick=0扱いへフォールバック

    せず、明示的に404で拒否する(誤ったonset_secが永続化されるのを防ぐ)。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, with_note=False)
    # あえてbeatmap.jsonを書かない。

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={
            "ops": [
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 0,
                    "duration_tick": 480,
                    "midi": 60,
                }
            ]
        },
    )
    assert resp.status_code == 404, resp.text
    assert "beatmap" in resp.json()["detail"]


def test_ops_422_when_beatmap_has_no_beats(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, with_note=False)
    storage.write_json(
        storage.beatmap_path(settings.workspace_dir, project_id),
        {
            "beats": [],
            "downbeats_sec": [],
            "time_signatures": [],
            "tempo_map": [],
            "confidence": 0.0,
        },
    )

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={
            "ops": [
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 0,
                    "duration_tick": 480,
                    "midi": 60,
                }
            ]
        },
    )
    assert resp.status_code == 422, resp.text


def test_ops_404_for_missing_project(client: TestClient) -> None:
    resp = client.post(
        "/api/projects/nonexistent/score/ops",
        json={"ops": [{"type": "note.delete", "note_ids": [1]}]},
    )
    assert resp.status_code == 404


def test_add_note_uses_beatmap_to_compute_onset_sec(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id, with_note=False)
    _write_beatmap_120bpm_4_4(settings, project_id)

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={
            "ops": [
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 480,
                    "duration_tick": 240,
                    "midi": 64,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    notes = resp.json()["parts"][0]["notes"]
    assert len(notes) == 1
    assert notes[0]["provenance"] == "user"
    assert notes[0]["onset_sec"] == 0.5  # 120bpmで480tick=0.5秒
    assert notes[0]["duration_sec"] == 0.25

    # 永続化も確認する。
    persisted = storage.read_json(
        storage.score_current_path(settings.workspace_dir, project_id)
    )
    assert len(persisted["parts"][0]["notes"]) == 1


def test_delete_note_persists_soft_delete(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)
    note_id = (
        ScoreService(workspace_dir=settings.workspace_dir)
        .read_score(project_id)
        .parts[0]
        .notes[0]
        .id
    )

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.delete", "note_ids": [note_id]}]},
    )
    assert resp.status_code == 200, resp.text
    note = resp.json()["parts"][0]["notes"][0]
    assert note["status"] == "deleted"
    assert note["provenance"] == "user"


def test_restore_note_persists_active_status(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#35: `note.restore`(`note.delete`の逆操作)がAPI経由で永続化されること。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)
    note_id = (
        ScoreService(workspace_dir=settings.workspace_dir)
        .read_score(project_id)
        .parts[0]
        .notes[0]
        .id
    )
    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.delete", "note_ids": [note_id]}]},
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.restore", "note_ids": [note_id]}]},
    )
    assert resp.status_code == 200, resp.text
    note = resp.json()["parts"][0]["notes"][0]
    assert note["status"] == "active"
    assert note["provenance"] == "user"


def test_invalid_op_returns_422_and_does_not_persist(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)
    before = storage.read_json(
        storage.score_current_path(settings.workspace_dir, project_id)
    )

    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [99999], "midi": 60}]},
    )
    assert resp.status_code == 422, resp.text

    after = storage.read_json(
        storage.score_current_path(settings.workspace_dir, project_id)
    )
    assert after == before  # 失敗したリクエストは何も永続化しない


def test_concurrent_modification_returns_409(
    client: TestClient,
    settings: Settings,
    tiny_wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回帰: リクエスト処理中に別プロセスがscore/current.jsonを書き換えた場合、

    lost-updateで上書きしない(worker/dsp_main.pyの各ステージと同じ楽観的
    並行性制御)。
    """
    import app.api.score as score_module

    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    score_path = storage.score_current_path(settings.workspace_dir, project_id)
    original_apply_ops = score_module.apply_ops

    def _apply_and_concurrently_modify(score, ops, anchors):
        # apply_ops呼び出し中(=raw_before読み取り後、書き込み前)に、別プロセスが
        # score/current.jsonを書き換えたことを模擬する。
        concurrent = storage.read_json(score_path)
        concurrent["meta"]["stages"]["_concurrent_marker"] = {"touched": True}
        storage.write_json(score_path, concurrent)
        return original_apply_ops(score, ops, anchors)

    monkeypatch.setattr(score_module, "apply_ops", _apply_and_concurrently_modify)

    note_id = (
        ScoreService(workspace_dir=settings.workspace_dir)
        .read_score(project_id)
        .parts[0]
        .notes[0]
        .id
    )
    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 61}]},
    )
    assert resp.status_code == 409, resp.text

    final = storage.read_json(score_path)
    assert final["meta"]["stages"].get("_concurrent_marker") == {"touched": True}
    assert final["parts"][0]["notes"][0]["midi"] == 60  # 更新は反映されない


def test_concurrent_undo_state_modification_returns_409(
    client: TestClient,
    settings: Settings,
    tiny_wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回帰(#32-M3レビュー指摘): score/current.jsonだけでなく

    score/undo_state.jsonも並行変更検知の対象に含める。含めないと、本
    リクエストがscore本体を書き込んでからundo_state.jsonを更新するまでの
    間に、別リクエスト(/score/undo等)がundo_state.jsonを読んで書き換える
    競合を見逃し、どちらか一方の更新が消える(lost update)。
    """
    import app.api.score as score_module

    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    undo_state_path = storage.score_undo_state_path(settings.workspace_dir, project_id)
    original_apply_ops = score_module.apply_ops

    def _apply_and_concurrently_modify_undo_state(score, ops, anchors):
        # apply_ops呼び出し中(=raw_undo_before読み取り後、書き込み前)に、別
        # リクエストがundo_state.jsonを書き換えたことを模擬する。
        storage.write_json(
            undo_state_path,
            {"done": [], "undone": [], "_concurrent_marker": True},
        )
        return original_apply_ops(score, ops, anchors)

    monkeypatch.setattr(
        score_module, "apply_ops", _apply_and_concurrently_modify_undo_state
    )

    note_id = (
        ScoreService(workspace_dir=settings.workspace_dir)
        .read_score(project_id)
        .parts[0]
        .notes[0]
        .id
    )
    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 61}]},
    )
    assert resp.status_code == 409, resp.text

    # スコア本体は書き換わっていない(undo_state側の競合で弾かれたため)。
    score = storage.read_json(
        storage.score_current_path(settings.workspace_dir, project_id)
    )
    assert score["parts"][0]["notes"][0]["midi"] == 60


def test_ops_succeeds_when_undo_state_json_is_corrupted(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#32-M3レビュー指摘、2巡目、HIGH): 破損したundo_state.json

    (不正なJSON)があっても、`/score/ops`は500にならず正常に処理を続ける。
    `_read_undo_state_raw`は`score_undo.read_undo_state`と同じく
    OSError/ValueErrorを握りつぶしてNoneへフォールバックする。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    undo_state_path = storage.score_undo_state_path(settings.workspace_dir, project_id)
    undo_state_path.parent.mkdir(parents=True, exist_ok=True)
    undo_state_path.write_text("{not valid json", encoding="utf-8")

    note_id = (
        ScoreService(workspace_dir=settings.workspace_dir)
        .read_score(project_id)
        .parts[0]
        .notes[0]
        .id
    )
    resp = client.post(
        f"/api/projects/{project_id}/score/ops",
        json={"ops": [{"type": "note.update", "note_ids": [note_id], "midi": 61}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parts"][0]["notes"][0]["midi"] == 61
