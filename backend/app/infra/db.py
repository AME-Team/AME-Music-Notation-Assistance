"""SQLite スキーマとワークスペース永続化基盤(#12, NFR-05)。

Score IR 本体は SQLite の行に分解せず JSON ファイルで持つ(§10.3)。ここで持つのは
プロジェクト・ジョブ・エージェント run のメタデータのみ。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    audio_format      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,  -- queued | running | succeeded | failed | cancelled
    progress    REAL NOT NULL DEFAULT 0.0,
    message     TEXT,
    params_json TEXT,
    exit_code   INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);

-- ★ M5以降で使うagent run の骨組みのみ(#12完了条件はM0の範囲に限定)。
CREATE TABLE IF NOT EXISTS agent_runs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_lock = threading.Lock()
# workspace(db_path)ごとにコネクションを保持する。プロセス内で複数のワークスペースを
# 同時に扱えるようにする(テストが workspace ごとに tmp_path を使うため必須)。
_connections: dict[str, sqlite3.Connection] = {}


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    fields = [col[0] for col in cursor.description]
    return dict(zip(fields, row, strict=True))


def get_connection(db_path: Path) -> sqlite3.Connection:
    """`db_path` ごとに共有するコネクションを返す(NFR-05: 再起動後も復元できる)。

    単一ユーザー・単一プロセス前提(§5.1)のため、書き込みはサービス層で
    直列化する想定(asyncio.Lock)。ここではスレッド安全性のみ担保する。
    """
    key = str(db_path.resolve())
    with _lock:
        if key not in _connections:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = _row_factory
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            _connections[key] = conn
        return _connections[key]


def close_connection(db_path: Path) -> None:
    """テストや再起動シミュレーション用に、特定ワークスペースのコネクションを閉じる。"""
    key = str(db_path.resolve())
    with _lock:
        conn = _connections.pop(key, None)
        if conn is not None:
            conn.close()
