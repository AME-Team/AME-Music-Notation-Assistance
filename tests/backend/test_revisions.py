"""リビジョン管理(#59, FR-15)の単体・APIテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo
from app.infra import storage
from app.main import create_app
from app.services.project_service import ProjectService
from app.services.revision_service import (
    RevisionNotFoundError,
    RevisionService,
)
from app.services.score_service import ScoreNotFoundError, ScoreService
from fastapi.testclient import TestClient


def _create_test_score(project_id: str, note_count: int = 3) -> ScoreIR:
    notes = [
        Note(
            id=i + 1,
            onset_sec=i * 0.5,
            duration_sec=0.5,
            midi=60 + i,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        )
        for i in range(note_count)
    ]
    return ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="test.wav", duration_sec=5.0, sample_rate=44100),
        parts=[
            Part(
                id="piano",
                name="Piano",
                midi_program=0,
                staves=2,
                clefs=[
                    Clef(staff=1, sign="G", line=2),
                    Clef(staff=2, sign="F", line=4),
                ],
                notes=notes,
            )
        ],
        next_note_id=note_count + 1,
    )


def test_revision_service_lifecycle(tmp_path: Path) -> None:
    project_service = ProjectService(workspace_dir=tmp_path)
    proj = project_service.create_project(
        original_filename="audio.wav", content=b"RIFFdummy"
    )
    project_id = proj["id"]
    score_service = ScoreService(workspace_dir=tmp_path)
    score_service.write_score(project_id, _create_test_score(project_id, note_count=3))

    service = RevisionService(workspace_dir=tmp_path)

    # リビジョン作成
    rev1 = service.create_revision(
        project_id, name="Initial Take", description="3 notes"
    )
    assert rev1.name == "Initial Take"
    assert rev1.description == "3 notes"
    assert rev1.op_count == 0
    assert rev1.id.startswith("rev_")

    # 一覧取得
    revs = service.list_revisions(project_id)
    assert len(revs) == 1
    assert revs[0].id == rev1.id

    # 個別取得
    fetched = service.get_revision(project_id, rev1.id)
    assert fetched.id == rev1.id
    assert fetched.name == "Initial Take"

    # 存在しないリビジョンの取得
    with pytest.raises(RevisionNotFoundError):
        service.get_revision(project_id, "rev_nonexistent")

    # スコアを更新(4音にする)
    score_service.write_score(project_id, _create_test_score(project_id, note_count=4))
    rev2 = service.create_revision(project_id, name="Updated Take")

    revs = service.list_revisions(project_id)
    assert len(revs) == 2
    assert revs[0].id == rev2.id  # 新しい順

    # 差分計算 (rev2 と rev1)
    diff = service.compute_revision_diff(project_id, rev2.id, base_revision_id=rev1.id)
    assert diff["parts"]["piano"]["base_note_count"] == 3
    assert diff["parts"]["piano"]["target_note_count"] == 4
    assert diff["parts"]["piano"]["added_notes"] == 1

    # rev1 へ復元
    restored_score = service.restore_revision(project_id, rev1.id)
    piano_restored = restored_score.find_part("piano")
    assert piano_restored is not None
    assert len(piano_restored.notes) == 3

    # 現在のスコアも復元されていること
    current_score = score_service.read_score(project_id)
    piano_current = current_score.find_part("piano")
    assert piano_current is not None
    assert len(piano_current.notes) == 3

    # 監査ログ(revisions.jsonl)に記録されていること
    rev_log_path = storage.score_revisions_log_path(tmp_path, project_id)
    assert rev_log_path.exists()
    rev_log_content = rev_log_path.read_text(encoding="utf-8")
    assert "revision.restore" in rev_log_content

    # 削除
    service.delete_revision(project_id, rev2.id)
    assert len(service.list_revisions(project_id)) == 1


def test_revision_service_raises_when_no_score(tmp_path: Path) -> None:
    project_id = "proj_no_score"
    storage.ensure_project_layout(tmp_path, project_id)
    service = RevisionService(workspace_dir=tmp_path)
    with pytest.raises(ScoreNotFoundError):
        service.create_revision(project_id, name="Fail")


def test_revisions_api_endpoints(tmp_path: Path) -> None:
    settings = Settings(
        host="127.0.0.1", port=8000, workspace_dir=tmp_path, auth_token="test-token"
    )
    app = create_app(settings)
    client = TestClient(app, headers={"X-AME-Token": "test-token"})

    project_service = ProjectService(workspace_dir=tmp_path)
    proj = project_service.create_project(
        original_filename="audio.wav", content=b"RIFFdummy"
    )
    project_id = proj["id"]

    score_service = ScoreService(workspace_dir=tmp_path)
    score_service.write_score(project_id, _create_test_score(project_id, note_count=2))

    # 作成
    resp = client.post(
        f"/api/projects/{project_id}/revisions",
        json={"name": "v1.0", "description": "First draft"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "v1.0"
    rev_id = data["id"]

    # 一覧
    resp = client.get(f"/api/projects/{project_id}/revisions")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 詳細
    resp = client.get(f"/api/projects/{project_id}/revisions/{rev_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == rev_id

    # 差分 (現在スコアとの差分)
    resp = client.get(f"/api/projects/{project_id}/revisions/{rev_id}/diff")
    assert resp.status_code == 200
    assert "parts" in resp.json()

    # 復元
    resp = client.post(f"/api/projects/{project_id}/revisions/{rev_id}/restore")
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project_id

    # 削除
    resp = client.delete(f"/api/projects/{project_id}/revisions/{rev_id}")
    assert resp.status_code == 204

    # 削除後は404
    resp = client.get(f"/api/projects/{project_id}/revisions/{rev_id}")
    assert resp.status_code == 404
