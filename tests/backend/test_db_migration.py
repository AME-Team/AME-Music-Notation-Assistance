"""#49 Gate2レビュー指摘(HIGH)の回帰テスト: agent_runsテーブルの列追加マイグレーション。

`CREATE TABLE IF NOT EXISTS`は既存テーブルの列を追加しないため、#46/#47時点で
作成済みの(turns/usage_json/staged_ops_count/error列が無い)agent_runsを持つ
既存ワークスペースに対して、`db.get_connection`が`ALTER TABLE`で不足列を
補えることを確認する。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.infra import db
from app.services.agent_run_service import AgentRunService
from app.services.project_service import ProjectService


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


def test_concurrent_reads_from_multiple_threads_do_not_fail(tmp_path: Path) -> None:
    """#150(Windows実機で発覚した500の回帰テスト): 複数スレッドからの同時アクセス。

    以前は`db_path`ごとに1つのコネクションを全スレッドで共有していたため、FastAPIの
    同期エンドポイントがスレッドプールで並行実行されると
    `sqlite3.InterfaceError: bad parameter or other API misuse`や
    `ProjectNotFoundError`(fetchoneがNone)が発生し、`GET /stems`が500になっていた。
    スレッドごとに独立したコネクションを返すことで解消している。
    """
    workspace = tmp_path
    service = ProjectService(workspace_dir=workspace)
    project = service.create_project(
        original_filename="concurrent.wav", content=b"RIFF0000WAVEfmt "
    )
    project_id = project["id"]

    errors: list[str] = []

    def worker() -> None:
        for _ in range(60):
            try:
                service.get_project(project_id)
                service.list_projects()
            except Exception as exc:  # noqa: BLE001 — 何が漏れるかを報告したい
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_each_thread_gets_its_own_connection(tmp_path: Path) -> None:
    """スレッドごとに別のコネクションを返し、同一スレッド内では再利用すること。"""
    db_path = tmp_path / "db.sqlite3"
    main_conn = db.get_connection(db_path)
    assert db.get_connection(db_path) is main_conn

    seen: list[int] = []

    def worker() -> None:
        seen.append(id(db.get_connection(db_path)))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen[0] != id(main_conn)
