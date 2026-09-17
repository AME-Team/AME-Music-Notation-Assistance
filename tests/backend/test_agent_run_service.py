"""#46: AgentRunService の単体テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.provider import AgentRunNotFoundError
from app.infra import storage
from app.services.agent_run_service import AgentRunService
from app.services.project_service import ProjectNotFoundError


def _create_project_dir(workspace_dir: Path, project_id: str) -> None:
    storage.ensure_project_layout(workspace_dir, project_id)
    # db.sqlite3 の projects テーブルにもレコードを挿入
    from app.infra import db

    conn = db.get_connection(workspace_dir / "db.sqlite3")
    conn.execute(
        "INSERT INTO projects(id, name, original_filename, audio_format, created_at) "
        "VALUES (?, 'Test', 'song.wav', 'wav', '2026-01-01T00:00:00Z')",
        (project_id,),
    )
    conn.commit()


class TestAgentRunService:
    def test_create_and_get_run(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        _create_project_dir(tmp_path, "proj_1")

        created = service.create_run(run_id="run_1", project_id="proj_1")
        assert created["id"] == "run_1"
        assert created["project_id"] == "proj_1"
        assert created["status"] == "running"

        fetched = service.get_run("run_1")
        assert fetched["id"] == "run_1"
        assert fetched["project_id"] == "proj_1"
        assert fetched["status"] == "running"

    def test_create_run_fails_for_nonexistent_project(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        with pytest.raises(ProjectNotFoundError):
            service.create_run(run_id="run_fail", project_id="nonexistent_proj")

    def test_create_run_fails_on_duplicate_run_id(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        _create_project_dir(tmp_path, "proj_1")
        service.create_run(run_id="run_dup", project_id="proj_1")

        with pytest.raises(ValueError, match="already exists"):
            service.create_run(run_id="run_dup", project_id="proj_1")

    def test_get_run_raises_for_unknown_run_id(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        with pytest.raises(AgentRunNotFoundError):
            service.get_run("unknown_run")

    def test_update_status(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        _create_project_dir(tmp_path, "proj_1")
        service.create_run(run_id="run_1", project_id="proj_1")

        updated = service.update_status("run_1", "completed")
        assert updated["status"] == "completed"

        # truncated もサポートされていること
        updated_truncated = service.update_status("run_1", "truncated")
        assert updated_truncated["status"] == "truncated"

    def test_update_status_raises_for_unknown_run(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        with pytest.raises(AgentRunNotFoundError):
            service.update_status("unknown_run", "completed")

    def test_list_runs(self, tmp_path: Path) -> None:
        service = AgentRunService(workspace_dir=tmp_path)
        _create_project_dir(tmp_path, "proj_1")
        _create_project_dir(tmp_path, "proj_2")

        service.create_run(run_id="run_a", project_id="proj_1")
        service.create_run(run_id="run_b", project_id="proj_1")
        service.create_run(run_id="run_c", project_id="proj_2")

        all_runs = service.list_runs()
        assert len(all_runs) == 3

        proj_1_runs = service.list_runs(project_id="proj_1")
        assert len(proj_1_runs) == 2
        assert {r["id"] for r in proj_1_runs} == {"run_a", "run_b"}
