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

-- #49: turns/usage_json/staged_ops_count/error は AgentRunManager が
-- provider.result() を受けて書き込む(§11.3 `GET /api/agent/runs/{run_id}`
-- レスポンスの実体)。
CREATE TABLE IF NOT EXISTS agent_runs (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status            TEXT NOT NULL,
    turns             INTEGER NOT NULL DEFAULT 0,
    usage_json        TEXT,
    staged_ops_count  INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        TEXT NOT NULL
);

-- #59: リビジョン管理 (FR-15, §10.3)
CREATE TABLE IF NOT EXISTS revisions (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    description    TEXT,
    op_count       INTEGER NOT NULL DEFAULT 0,
    score_snapshot TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_project ON revisions(project_id);
"""

# #150(Windows実機で発覚した500の原因): **スレッドごと**にコネクションを保持する。
#
# 以前は`db_path`ごとに1つのコネクションを全スレッドで共有していた。sqlite3の
# コネクション/カーソルはスレッド間で同時使用すると
# `sqlite3.InterfaceError: bad parameter or other API misuse`を送出する
# (または`fetchone()`がNoneを返して`ProjectNotFoundError`になる)ため、FastAPIの
# 同期エンドポイントがスレッドプールで並行実行されると`GET /stems`等が500になった。
# 実際に8スレッドから`get_project`/`list_projects`を叩くと95件のエラーで再現した。
#
# スレッドローカルにすることで、接続は常に単一スレッドからしか触られない
# (SQLiteはWALモードで複数接続の並行読み書きを許容する)。
_thread_local = threading.local()
# 同一パスの同時作成を避けるためのロック(`PRAGMA journal_mode=WAL`は排他が必要)。
_create_lock = threading.Lock()


def _thread_connections() -> dict[str, sqlite3.Connection]:
    """現スレッドが保持する`db_path -> connection`のマップ。"""
    store = getattr(_thread_local, "by_path", None)
    if store is None:
        store = {}
        _thread_local.by_path = store
    return store


# #49 Gate2レビュー指摘(HIGH): `CREATE TABLE IF NOT EXISTS`は既存テーブルの
# 列を追加しない。#46/#47時点で作成済みの`agent_runs`(turns/usage_json/
# staged_ops_count/error列が無い)を持つ既存ワークスペースでは、そのまま
# `get_run`/`update_status`/`list_runs`を呼ぶと`no such column`で失敗する。
# ここに簡易マイグレーション(不足列の検出→`ALTER TABLE ADD COLUMN`)を持つ。
_AGENT_RUNS_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("turns", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_json", "TEXT"),
    ("staged_ops_count", "INTEGER NOT NULL DEFAULT 0"),
    ("error", "TEXT"),
)


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    fields = [col[0] for col in cursor.description]
    return dict(zip(fields, row, strict=True))


def _migrate_agent_runs_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    for name, ddl in _AGENT_RUNS_MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {ddl}")  # noqa: S608


def _create_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = _row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # スレッドごとに独立した接続になるため、書き込みの同時実行で
    # "database is locked"にならないよう待機時間を持たせる(#150)。
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    _migrate_agent_runs_columns(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def get_connection(db_path: Path) -> sqlite3.Connection:
    """現スレッド用のコネクションを返す(NFR-05: 再起動後も復元できる)。

    単一ユーザー・単一プロセス前提(§5.1)だが、FastAPIの同期エンドポイントは
    スレッドプールで並行実行されるため、**コネクションをスレッド間で共有しない**
    (共有すると`sqlite3.InterfaceError`や`ProjectNotFoundError`が起きる。詳細は
    モジュール冒頭のコメント参照)。同じスレッド内では同じコネクションを再利用する。
    """
    key = str(db_path.resolve())
    store = _thread_connections()
    conn = store.get(key)
    if conn is not None:
        return conn
    with _create_lock:
        # `store`はスレッドローカルなのでロック待ち中に他スレッドが書き込むことはない。
        # ここでの再確認は同一スレッドからの再入に対する防御。
        conn = store.get(key)
        if conn is None:
            conn = _create_connection(db_path)
            store[key] = conn
        return conn


def close_connection(db_path: Path) -> None:
    """テストや再起動シミュレーション用に、**現スレッドが持つ**コネクションを閉じる。

    スレッドごとに独立した接続を持つため、他のスレッドの接続はここでは閉じられない
    (各スレッドの接続はそのスレッドの終了時にプロセスごと破棄される)。
    """
    key = str(db_path.resolve())
    conn = _thread_connections().pop(key, None)
    if conn is not None:
        conn.close()


def close_all_connections() -> None:
    """現スレッドが保持する全コネクションを閉じる(プロセス終了時の後始末用)。"""
    store = _thread_connections()
    for conn in store.values():
        conn.close()
    store.clear()
