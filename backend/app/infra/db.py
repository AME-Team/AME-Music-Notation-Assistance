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
# `db_path -> (Thread -> connection)`。スレッドごとに独立した接続を持ちつつ、
# **ワークスペース単位で全スレッドの接続を閉じられる**ようにするためのレジストリ
# (#150レビュー指摘: スレッドローカルだけだと`close_connection`が別スレッドから
# 呼ばれた場合に黙って何もしなくなり、接続がリークする)。
#
# キーは`threading.get_ident()`ではなく**Threadオブジェクト**にする: identは
# スレッド終了後に**再利用される**ため、死んだスレッドの接続を新しいスレッドへ
# 誤って渡してしまう(`check_same_thread=True`でProgrammingErrorになる)ことを
# 実際にテストで踏んだ。
_connections: dict[str, dict[threading.Thread, sqlite3.Connection]] = {}
# レジストリと接続生成の保護(`PRAGMA journal_mode=WAL`は排他が必要)。
_registry_lock = threading.Lock()


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
    # `check_same_thread=False`を維持する(#150レビュー指摘への回答):
    # スレッド局所性はレジストリ(キーがThreadオブジェクト)と「接続を他スレッドへ
    # 渡さない」ことで構造的に担保しており、SQLite側のチェックに頼っていない。
    # 一方`True`にすると、**別スレッド(既に終了したスレッドを含む)が作った接続を
    # 閉じることすら出来なくなり**(`conn.close()`がProgrammingError)、
    # ワークスペース単位の後始末(`close_connection`/`close_all_connections`)が
    # 成立しない。両案は排他なので「終了時に確実に閉じられる」を優先する。
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
    thread = threading.current_thread()
    with _registry_lock:
        by_thread = _connections.setdefault(key, {})
        conn = by_thread.get(thread)
        if conn is None:
            conn = _create_connection(db_path)
            by_thread[thread] = conn
        return conn


def close_connection(db_path: Path) -> None:
    """特定ワークスペースのコネクションを**全スレッド分**閉じる(テストの後始末・再起動シミュレーション用)。

    呼び出し元のスレッドに関わらず閉じられる(レジストリで管理しているため)。
    ただし**使用中の接続を閉じる**ことになるため、並行アクセス中には呼ばないこと。
    """
    key = str(db_path.resolve())
    with _registry_lock:
        by_thread = _connections.pop(key, {})
    for conn in by_thread.values():
        conn.close()


def close_all_connections() -> None:
    """プロセス内の全コネクションを閉じる(アプリ終了時の後始末。`app.main.lifespan`から呼ぶ)。"""
    with _registry_lock:
        all_connections = list(_connections.values())
        _connections.clear()
    for by_thread in all_connections:
        for conn in by_thread.values():
            conn.close()
