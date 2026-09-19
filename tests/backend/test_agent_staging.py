"""#46: L2 Coding Agent のステージング領域と承認・キャンセルフローのテスト(FR-23)。

完了条件:
- 実行途中でキャンセルしても current.json が 1 バイトも変わらないことをテストで確認
- 上限到達時(truncated)はステージングされた変更を破棄せず提示する(§8.9)
"""

from __future__ import annotations

import hashlib
import json

import pytest
from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo
from app.infra import storage
from app.services.agent_run_service import AgentRunService
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient

_RUN_ID = "run_staging_test_01"


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _setup_score_and_staging(
    settings: Settings, project_id: str, run_id: str, status: str = "running"
) -> tuple[ScoreIR, ScoreIR, bytes]:
    """current.json と staging/{run_id}.json を作成し、current.json の生バイト列を返す。"""
    agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
    agent_service.create_run(run_id=run_id, project_id=project_id, status=status)  # type: ignore[arg-type]

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
    note1 = Note(
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
    note2 = Note(
        id=score.allocate_note_id(),
        onset_sec=0.5,
        duration_sec=0.5,
        onset_tick=480,
        duration_tick=480,
        midi=64,
        velocity=90,
        provenance="baseline",
        voice=1,
        staff=1,
    )
    part.notes.extend([note1, note2])
    score.parts.append(part)
    ScoreService(workspace_dir=settings.workspace_dir).write_score(project_id, score)

    # current.json の初期生バイト列を保存
    current_path = storage.score_current_path(settings.workspace_dir, project_id)
    initial_bytes = current_path.read_bytes()

    # ステージングファイルを用意(エージェントが score_apply_ops を実行した状態を再現)
    staged = score.model_copy(deep=True)
    staged_part = staged.find_part("piano")
    assert staged_part is not None
    staged_note1, staged_note2 = staged_part.notes
    staged_note1.voice = 2
    staged_note1.provenance = "llm"
    staged_note1.provenance_run_id = run_id
    staged_note1.ai_reason = "声部分離"
    staged_note2.status = "deleted"
    staged_note2.provenance = "llm"
    staged_note2.provenance_run_id = run_id
    staged_note2.ai_reason = "ゴースト音削除"

    staging_path = storage.score_staging_path(
        settings.workspace_dir, project_id, run_id
    )
    storage.write_json(staging_path, staged.model_dump(mode="json"))

    return score, staged, initial_bytes


class TestAgentCancelStaging:
    def test_cancel_leaves_current_json_byte_for_byte_unmodified(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """#46 完了条件: 実行途中でキャンセルしても current.json が 1 バイトも変わらないことを確認。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _, _, initial_bytes = _setup_score_and_staging(settings, project_id, _RUN_ID)
        initial_hash = hashlib.sha256(initial_bytes).hexdigest()

        # キャンセル API 呼び出し
        resp = client.post(f"/api/agent/runs/{_RUN_ID}/cancel")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["run_id"] == _RUN_ID
        assert data["status"] == "cancelled"

        # current.json が 1 バイトも変わっていないことを SHA-256 とバイト列で完全一致検証
        current_path = storage.score_current_path(settings.workspace_dir, project_id)
        current_bytes = current_path.read_bytes()
        assert current_bytes == initial_bytes
        assert hashlib.sha256(current_bytes).hexdigest() == initial_hash

    def test_cancel_deletes_staging_file_and_updates_status(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """キャンセル時にステージングファイルが破棄され、ステータスが更新され、監査ログが残る。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, _RUN_ID)

        staging_path = storage.score_staging_path(
            settings.workspace_dir, project_id, _RUN_ID
        )
        assert staging_path.exists()

        resp = client.post(f"/api/agent/runs/{_RUN_ID}/cancel")
        assert resp.status_code == 200

        # ステージングファイルが削除されていること
        assert not staging_path.exists()

        # DB 上のステータスが cancelled に更新されていること
        agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
        run_record = agent_service.get_run(_RUN_ID)
        assert run_record["status"] == "cancelled"

        # 監査ログ(ops.jsonl)に ai.cancel が記録されていること
        ops_log_path = storage.score_ops_log_path(settings.workspace_dir, project_id)
        assert ops_log_path.exists()
        entries = [
            json.loads(line)
            for line in ops_log_path.read_text(encoding="utf-8").splitlines()
        ]
        cancel_entries = [e for e in entries if e["ops"][0]["op"] == "ai.cancel"]
        assert len(cancel_entries) == 1
        assert cancel_entries[0]["ops"][0]["run_id"] == _RUN_ID

    def test_cancel_is_safe_when_no_staging_file(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """エージェントが score_apply_ops を実行する前にキャンセルした場合でも正常に処理される。"""
        project_id = _create_project(client, tiny_wav_bytes)
        agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
        agent_service.create_run(run_id="run_no_staging", project_id=project_id)

        resp = client.post("/api/agent/runs/run_no_staging/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


class TestAgentDiffAndApproval:
    def test_get_diff_returns_staged_changes(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """GET /api/agent/runs/{run_id}/diff でステージングと current の差分が取得できる。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, _RUN_ID)

        resp = client.get(f"/api/agent/runs/{_RUN_ID}/diff")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["run_id"] == _RUN_ID
        assert len(data["changes"]) == 2
        types = {c["change_type"] for c in data["changes"]}
        assert types == {"keep", "delete"}

    def test_get_diff_returns_empty_when_no_staging_file(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """ステージングファイル未作成の場合は空の差分を返す。"""
        project_id = _create_project(client, tiny_wav_bytes)
        # スコアを作成
        score = ScoreIR(
            project_id=project_id,
            source=SourceInfo(filename="song.wav", duration_sec=1.0, sample_rate=8000),
        )
        ScoreService(workspace_dir=settings.workspace_dir).write_score(
            project_id, score
        )

        agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
        agent_service.create_run(run_id="run_empty", project_id=project_id)

        resp = client.get("/api/agent/runs/run_empty/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run_empty"
        assert data["changes"] == []

    def test_accept_applies_all_changes_to_current(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """POST /api/agent/runs/{run_id}/accept で変更が current.json に確定反映される。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, _RUN_ID)

        resp = client.post(f"/api/agent/runs/{_RUN_ID}/accept", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["applied_note_ids"]) == 2
        assert len(data["remaining_diff"]["changes"]) == 0

        # current.json に反映されていること
        current = ScoreService(workspace_dir=settings.workspace_dir).read_score(
            project_id
        )
        part = current.find_part("piano")
        assert part is not None
        assert part.notes[0].voice == 2
        assert part.notes[1].status == "deleted"

        # 監査ログに ai.accept が記録されていること
        ops_log_path = storage.score_ops_log_path(settings.workspace_dir, project_id)
        entries = [
            json.loads(line)
            for line in ops_log_path.read_text(encoding="utf-8").splitlines()
        ]
        accept_entries = [e for e in entries if e["ops"][0]["op"] == "ai.accept"]
        assert len(accept_entries) == 1

    def test_accept_with_scope_applies_partial_changes(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """スコープ指定による部分承認(FR-23)。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, _RUN_ID)

        # 1小節目のみ承認
        resp = client.post(
            f"/api/agent/runs/{_RUN_ID}/accept",
            json={"part_id": "piano", "bar_range": [1, 1]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["applied_note_ids"]) == 2  # 両ノートとも1小節目にあるため適用

    def test_reject_reverts_changes_in_staging(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """POST /api/agent/runs/{run_id}/reject でステージングが巻き戻り、current は不変。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _, _, initial_bytes = _setup_score_and_staging(settings, project_id, _RUN_ID)

        resp = client.post(f"/api/agent/runs/{_RUN_ID}/reject", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["reverted_note_ids"]) == 2
        assert len(data["remaining_diff"]["changes"]) == 0

        # current.json は 1 バイトも変わっていないこと
        current_path = storage.score_current_path(settings.workspace_dir, project_id)
        assert current_path.read_bytes() == initial_bytes

        # 監査ログに ai.reject が記録されていること
        ops_log_path = storage.score_ops_log_path(settings.workspace_dir, project_id)
        entries = [
            json.loads(line)
            for line in ops_log_path.read_text(encoding="utf-8").splitlines()
        ]
        reject_entries = [e for e in entries if e["ops"][0]["op"] == "ai.reject"]
        assert len(reject_entries) == 1

    def test_accept_transitions_status_to_completed(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """全差分が確定適用された時点で run の status が completed へ遷移すること(#46 レビュー対応)。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, "run_accept_complete")

        resp = client.post("/api/agent/runs/run_accept_complete/accept", json={})
        assert resp.status_code == 200
        assert len(resp.json()["remaining_diff"]["changes"]) == 0

        agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
        run_record = agent_service.get_run("run_accept_complete")
        assert run_record["status"] == "completed"

    def test_reject_transitions_status_to_completed(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """全差分が却下された時点で run の status が completed へ遷移すること(#46 レビュー対応)。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, "run_reject_complete")

        resp = client.post("/api/agent/runs/run_reject_complete/reject", json={})
        assert resp.status_code == 200
        assert len(resp.json()["remaining_diff"]["changes"]) == 0

        agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
        run_record = agent_service.get_run("run_reject_complete")
        assert run_record["status"] == "completed"

    def test_reject_aborts_on_concurrent_modification(
        self,
        client: TestClient,
        settings: Settings,
        tiny_wav_bytes: bytes,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reject 実行中に current.json が並行更新されていた場合 409 になり、上書きを防止する(#46 レビュー対応)。"""
        import app.services.agent_run_service as service_module

        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(settings, project_id, "run_reject_conflict")

        score_path = storage.score_current_path(settings.workspace_dir, project_id)
        original_revert = service_module.revert_change_in_staging

        def _revert_and_concurrently_modify(change, *, current, staged):
            # raw_before 読み取り後、staging 保存前に current.json を並行更新する
            concurrent = storage.read_json(score_path)
            concurrent["divisions"] = 960
            storage.write_json(score_path, concurrent)
            return original_revert(change, current=current, staged=staged)

        monkeypatch.setattr(
            service_module, "revert_change_in_staging", _revert_and_concurrently_modify
        )

        resp = client.post("/api/agent/runs/run_reject_conflict/reject", json={})
        assert resp.status_code == 409
        assert "concurrently" in resp.json()["detail"]

    def test_cannot_cancel_already_completed_run(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """承認完了済みの completed run をキャンセルしようとすると 400 エラーになり上書きされない(#46 レビュー対応)。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(
            settings, project_id, "run_already_completed", status="completed"
        )

        resp = client.post("/api/agent/runs/run_already_completed/cancel")
        assert resp.status_code == 400
        assert "cannot cancel an already completed" in resp.json()["detail"]

        # status が completed のままであること
        agent_service = AgentRunService(workspace_dir=settings.workspace_dir)
        assert agent_service.get_run("run_already_completed")["status"] == "completed"


class TestAgentTruncatedRun:
    def test_truncated_run_preserves_staging_and_allows_diff_and_accept(
        self, client: TestClient, settings: Settings, tiny_wav_bytes: bytes
    ) -> None:
        """#46: 上限到達時(truncated)はステージングを破棄せず保持し、部分成果として提示・承認可能(§8.9)。"""
        project_id = _create_project(client, tiny_wav_bytes)
        _setup_score_and_staging(
            settings, project_id, "run_truncated_01", status="truncated"
        )

        # 1. diff でステージング差分が破棄されず提示されること
        diff_resp = client.get("/api/agent/runs/run_truncated_01/diff")
        assert diff_resp.status_code == 200
        data = diff_resp.json()
        assert len(data["changes"]) == 2

        # 2. 部分成果を承認できること
        accept_resp = client.post("/api/agent/runs/run_truncated_01/accept", json={})
        assert accept_resp.status_code == 200
        assert len(accept_resp.json()["applied_note_ids"]) == 2

        # 3. current.json に反映されたこと
        current = ScoreService(workspace_dir=settings.workspace_dir).read_score(
            project_id
        )
        part = current.find_part("piano")
        assert part is not None
        assert part.notes[0].voice == 2


class TestAgentStagingErrors:
    def test_unknown_run_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/agent/runs/unknown_run/diff").status_code == 404
        assert (
            client.post("/api/agent/runs/unknown_run/accept", json={}).status_code
            == 404
        )
        assert (
            client.post("/api/agent/runs/unknown_run/reject", json={}).status_code
            == 404
        )
        assert client.post("/api/agent/runs/unknown_run/cancel").status_code == 404
