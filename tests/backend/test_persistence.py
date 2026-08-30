"""#12: 全ステージ成果物をディスクに永続化し、アプリ再起動後も途中から再開できること(NFR-05)。"""

from __future__ import annotations

from pathlib import Path

from app.infra import db
from app.services.project_service import ProjectService


def test_project_survives_process_restart_simulation(
    workspace_dir: Path, tiny_wav_bytes: bytes
) -> None:
    service_before = ProjectService(workspace_dir=workspace_dir)
    project = service_before.create_project(
        original_filename="restart.wav", content=tiny_wav_bytes
    )
    project_id = project["id"]

    # 「プロセス再起動」をシミュレートする: DBコネクションを閉じ、新しい
    # ProjectService インスタンス(=新しいコネクション)で読み直す。
    db.close_connection(workspace_dir / "db.sqlite3")
    service_after = ProjectService(workspace_dir=workspace_dir)

    restored = service_after.get_project(project_id)
    assert restored["id"] == project_id
    assert restored["original_filename"] == "restart.wav"

    projects = service_after.list_projects()
    assert any(p["id"] == project_id for p in projects)

    # 音声ファイル自体もディスクに残っている(NFR-05)。
    assert service_after.audio_path(project_id).exists()


def test_japanese_filename_round_trips(
    workspace_dir: Path, tiny_wav_bytes: bytes
) -> None:
    """#79: 日本語ファイル名でも UTF-8 で正しく保存・復元できること。"""
    service = ProjectService(workspace_dir=workspace_dir)
    project = service.create_project(
        original_filename="曲名テスト.wav", content=tiny_wav_bytes
    )

    db.close_connection(workspace_dir / "db.sqlite3")
    reloaded = ProjectService(workspace_dir=workspace_dir).get_project(project["id"])
    assert reloaded["original_filename"] == "曲名テスト.wav"
