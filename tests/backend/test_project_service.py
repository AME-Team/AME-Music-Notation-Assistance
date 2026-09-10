"""#29: ProjectService._with_stage_summary()のstale判定(FR-14完了条件)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.infra import storage
from app.services.project_service import ProjectService


def _insert_job(
    service: ProjectService, project_id: str, stage: str, status: str, *, job_id: str
) -> None:
    now = datetime.now(UTC).isoformat()
    conn = service._conn()
    conn.execute(
        "INSERT INTO jobs(id, project_id, stage, status, progress, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            project_id,
            stage,
            status,
            1.0 if status == "succeeded" else 0.0,
            now,
            now,
        ),
    )
    conn.commit()


def test_stage_is_stale_when_succeeded_but_metadata_missing(
    workspace_dir: Path, tiny_wav_bytes: bytes
) -> None:
    """直近ジョブが成功しているのにmeta.jsonが無ければ、上流の再実行/手動編集で

    無効化された(要再実行)状態として`stale: True`を返す。
    """
    service = ProjectService(workspace_dir=workspace_dir)
    project = service.create_project(
        original_filename="song.wav", content=tiny_wav_bytes
    )
    project_id = project["id"]

    _insert_job(service, project_id, "quantize", "succeeded", job_id="job1")

    result = service.get_project(project_id)
    assert result["stages"]["quantize"]["stale"] is True


def test_stage_is_not_stale_when_metadata_present(
    workspace_dir: Path, tiny_wav_bytes: bytes
) -> None:
    service = ProjectService(workspace_dir=workspace_dir)
    project = service.create_project(
        original_filename="song.wav", content=tiny_wav_bytes
    )
    project_id = project["id"]

    _insert_job(service, project_id, "quantize", "succeeded", job_id="job1")
    storage.write_stage_metadata(
        workspace_dir, project_id, "quantize", params_hash="abc", provider_versions={}
    )

    result = service.get_project(project_id)
    assert result["stages"]["quantize"]["stale"] is False


def test_stage_is_not_stale_when_not_yet_succeeded(
    workspace_dir: Path, tiny_wav_bytes: bytes
) -> None:
    """実行中/失敗したジョブはmeta.jsonが無くて当然であり、staleとは区別する。"""
    service = ProjectService(workspace_dir=workspace_dir)
    project = service.create_project(
        original_filename="song.wav", content=tiny_wav_bytes
    )
    project_id = project["id"]

    _insert_job(service, project_id, "quantize", "running", job_id="job1")

    result = service.get_project(project_id)
    assert result["stages"]["quantize"]["stale"] is False
