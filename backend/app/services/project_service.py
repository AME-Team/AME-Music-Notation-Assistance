"""プロジェクト CRUD のユースケース層(#11, FR-01)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.infra import db, ids, storage

ALLOWED_FORMATS = {"mp3", "wav", "flac", "m4a"}


class UnsupportedAudioFormatError(ValueError):
    pass


class ProjectNotFoundError(LookupError):
    pass


@dataclass
class ProjectService:
    workspace_dir: Path

    def _conn(self) -> sqlite3.Connection:
        return db.get_connection(self.workspace_dir / "db.sqlite3")

    def create_project(self, *, original_filename: str, content: bytes) -> dict:
        ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
        if ext not in ALLOWED_FORMATS:
            raise UnsupportedAudioFormatError(
                f"unsupported audio format: {ext!r} (allowed: {sorted(ALLOWED_FORMATS)})"
            )

        project_id = ids.new_id("proj")
        storage.ensure_project_layout(self.workspace_dir, project_id)
        audio_path = storage.original_audio_path(self.workspace_dir, project_id, ext)
        audio_path.write_bytes(content)

        created_at = datetime.now(UTC).isoformat()
        conn = self._conn()
        conn.execute(
            "INSERT INTO projects(id, name, original_filename, audio_format, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, original_filename, original_filename, ext, created_at),
        )
        conn.commit()
        return self.get_project(project_id)

    def list_projects(self) -> list[dict]:
        rows = (
            self._conn()
            .execute(
                "SELECT id, name, original_filename, audio_format, created_at "
                "FROM projects ORDER BY created_at DESC"
            )
            .fetchall()
        )
        return [self._with_stage_summary(row) for row in rows]

    def get_project(self, project_id: str) -> dict:
        row = (
            self._conn()
            .execute(
                "SELECT id, name, original_filename, audio_format, created_at "
                "FROM projects WHERE id = ?",
                (project_id,),
            )
            .fetchone()
        )
        if row is None:
            raise ProjectNotFoundError(project_id)
        return self._with_stage_summary(row)

    def _with_stage_summary(self, row: dict) -> dict:
        jobs = (
            self._conn()
            .execute(
                """
            SELECT stage, status, progress
            FROM jobs
            WHERE project_id = ? AND id IN (
                SELECT id FROM jobs j2
                WHERE j2.project_id = jobs.project_id AND j2.stage = jobs.stage
                ORDER BY created_at DESC LIMIT 1
            )
            """,
                (row["id"],),
            )
            .fetchall()
        )
        return {
            **row,
            "stages": {
                j["stage"]: {
                    "status": j["status"],
                    "progress": j["progress"],
                    # #29: 直近ジョブが成功しているのにmeta.jsonが無ければ、上流の
                    # 再実行/手動編集で無効化された(要再実行)状態。
                    "stale": j["status"] == "succeeded"
                    and not storage.stage_metadata_path(
                        self.workspace_dir, row["id"], j["stage"]
                    ).exists(),
                }
                for j in jobs
            },
        }

    def audio_path(self, project_id: str) -> Path:
        return self.audio_path_for_project(self.get_project(project_id))

    def audio_path_for_project(self, project: dict) -> Path:
        """`get_project()` 等で取得済みのレコードから原曲パスを解決する。

        `audio_path()` はこれを内部で使う薄いラッパー。呼び出し元が既に
        プロジェクトレコードを持っている場合(例: `api/media.py` の
        `get_peaks` が `_ensure_project_exists` の戻り値を再利用するケース)、
        `audio_path()` 経由だと `get_project` が再度実行されDBラウンドトリップが
        重複してしまう。API層がパス解決ロジック(`storage.original_audio_path`
        の呼び出し方)を独自に再実装して二重管理になるのを避けるため、この
        メソッドをサービス層に用意する(#21-M1レビュー指摘の追加ラウンド)。
        """
        return storage.original_audio_path(
            self.workspace_dir, project["id"], project["audio_format"]
        )

    def delete_project(self, project_id: str) -> None:
        self.get_project(project_id)  # raises if missing
        conn = self._conn()
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        storage.delete_project_dir(self.workspace_dir, project_id)
