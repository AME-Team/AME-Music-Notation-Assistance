"""#49 Gate2レビュー指摘(HIGH)の回帰テスト: agent_runsテーブルの列追加マイグレーション。

`CREATE TABLE IF NOT EXISTS`は既存テーブルの列を追加しないため、#46/#47時点で
作成済みの(turns/usage_json/staged_ops_count/error列が無い)agent_runsを持つ
既存ワークスペースに対して、`db.get_connection`が`ALTER TABLE`で不足列を
補えることを確認する。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.infra import db
from app.services.agent_run_service import AgentRunService


def _create_legacy_agent_runs_db(db_path: Path) -> None:
    """#46時点のagent_runsスキーマ(新規4列が無い)を直接作成する。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE agent_runs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "original_filename TEXT NOT NULL, audio_format TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('proj_1', 'Test', 'song.wav', 'wav', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agent_runs(id, project_id, status, created_at) "
        "VALUES ('run_legacy', 'proj_1', 'completed', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()


def test_get_connection_adds_missing_agent_runs_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _create_legacy_agent_runs_db(db_path)

    conn = db.get_connection(db_path)

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
    }
    assert {"turns", "usage_json", "staged_ops_count", "error"} <= columns

    row = conn.execute("SELECT * FROM agent_runs WHERE id = 'run_legacy'").fetchone()
    assert row["turns"] == 0
    assert row["usage_json"] is None
    assert row["staged_ops_count"] == 0
    assert row["error"] is None


def test_agent_run_service_works_against_migrated_legacy_db(tmp_path: Path) -> None:
    """AgentRunService層からも、#46時点のDBに対して#49の新規カラムが問題なく使えることを確認する。"""
    db_path = tmp_path / "db.sqlite3"
    _create_legacy_agent_runs_db(db_path)

    service = AgentRunService(workspace_dir=tmp_path)
    run = service.get_run("run_legacy")
    assert run["status"] == "completed"
    assert run["usage"] is None

    updated = service.update_status(
        "run_legacy", "completed", turns=5, staged_ops_count=2
    )
    assert updated["turns"] == 5
    assert updated["staged_ops_count"] == 2
