"""リビジョン管理サービス(#59, FR-15, §10.3, §10.4)。

名前付きリビジョンの作成・一覧・復元・差分取得を提供する。
リビジョンメタデータは SQLite (workspace/db.sqlite3) に記録され、
スコアのスナップショットおよび ops.jsonl の行数(op_count)を保持する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.score import ScoreIR
from app.infra import db, ids, storage
from app.services import score_undo
from app.services.score_service import ScoreNotFoundError, ScoreService
from app.services.stage_invalidation import invalidate_downstream


@dataclass(frozen=True)
class Revision:
    id: str
    project_id: str
    name: str
    description: str | None
    op_count: int
    score_snapshot: str
    created_at: str

    def to_dict(self, include_snapshot: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "op_count": self.op_count,
            "created_at": self.created_at,
        }
        if include_snapshot:
            data["score_snapshot"] = self.score_snapshot
        return data


class RevisionNotFoundError(Exception):
    def __init__(self, revision_id: str) -> None:
        super().__init__(f"revision {revision_id!r} not found")
        self.revision_id = revision_id


class RevisionService:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.db_path = workspace_dir / "db.sqlite3"

    def _get_conn(self):
        return db.get_connection(self.db_path)

    def _count_ops_lines(self, project_id: str) -> int:
        ops_path = storage.score_ops_log_path(self.workspace_dir, project_id)
        if not ops_path.exists():
            return 0
        try:
            with ops_path.open("r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def create_revision(
        self, project_id: str, name: str, description: str | None = None
    ) -> Revision:
        score_service = ScoreService(workspace_dir=self.workspace_dir)
        score = score_service.read_score(project_id)
        if score is None:
            raise ScoreNotFoundError(project_id)

        op_count = self._count_ops_lines(project_id)
        rev_id = ids.new_id("rev")
        now = datetime.now(UTC).isoformat()
        score_snapshot = score.model_dump_json()

        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO revisions (
                id, project_id, name, description, op_count, score_snapshot, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rev_id, project_id, name, description, op_count, score_snapshot, now),
        )
        conn.commit()

        return Revision(
            id=rev_id,
            project_id=project_id,
            name=name,
            description=description,
            op_count=op_count,
            score_snapshot=score_snapshot,
            created_at=now,
        )

    def list_revisions(self, project_id: str) -> list[Revision]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT id, project_id, name, description, op_count, score_snapshot, created_at
            FROM revisions
            WHERE project_id = ?
            ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
        return [
            Revision(
                id=r["id"],
                project_id=r["project_id"],
                name=r["name"],
                description=r["description"],
                op_count=r["op_count"],
                score_snapshot=r["score_snapshot"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_revision(self, project_id: str, revision_id: str) -> Revision:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT id, project_id, name, description, op_count, score_snapshot, created_at
            FROM revisions
            WHERE project_id = ? AND id = ?
            """,
            (project_id, revision_id),
        ).fetchone()
        if row is None:
            raise RevisionNotFoundError(revision_id)
        return Revision(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            op_count=row["op_count"],
            score_snapshot=row["score_snapshot"],
            created_at=row["created_at"],
        )

    def delete_revision(self, project_id: str, revision_id: str) -> None:
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM revisions WHERE project_id = ? AND id = ?",
            (project_id, revision_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise RevisionNotFoundError(revision_id)

    def restore_revision(self, project_id: str, revision_id: str) -> ScoreIR:
        rev = self.get_revision(project_id, revision_id)
        score = ScoreIR.model_validate_json(rev.score_snapshot)

        # 現在のスコアを上書き保存
        score_service = ScoreService(workspace_dir=self.workspace_dir)
        score_service.write_score(project_id, score)

        # Undo/Redoスタックをリセット(#32, §10.4)
        score_undo.reset_undo_state(storage.score_undo_state_path(self.workspace_dir, project_id))

        # リビジョン操作監査ログ(score/revisions.jsonl)に復元操作を追記
        log_path = storage.score_revisions_log_path(self.workspace_dir, project_id)
        entry = {
            "op": "revision.restore",
            "revision_id": rev.id,
            "revision_name": rev.name,
            "actor": "user",
            "ts": datetime.now(UTC).isoformat(),
        }
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # 下流ステージのメタデータを無効化
        invalidate_downstream(self.workspace_dir, project_id, "quantize")

        return score

    def compute_revision_diff(
        self, project_id: str, revision_id: str, base_revision_id: str | None = None
    ) -> dict[str, Any]:
        target_rev = self.get_revision(project_id, revision_id)
        target_score = ScoreIR.model_validate_json(target_rev.score_snapshot)

        if base_revision_id is not None:
            base_rev = self.get_revision(project_id, base_revision_id)
            base_score = ScoreIR.model_validate_json(base_rev.score_snapshot)
        else:
            score_service = ScoreService(workspace_dir=self.workspace_dir)
            base_score = score_service.read_score(project_id)
            if base_score is None:
                raise ScoreNotFoundError(project_id)

        # パートごとのノート数や統計差分を算出
        base_parts = {p.id: p for p in base_score.parts}
        target_parts = {p.id: p for p in target_score.parts}
        all_part_ids = sorted(set(base_parts.keys()) | set(target_parts.keys()))

        part_diffs: dict[str, Any] = {}
        for pid in all_part_ids:
            bp = base_parts.get(pid)
            tp = target_parts.get(pid)
            b_notes = {n.id: n for n in bp.notes} if bp else {}
            t_notes = {n.id: n for n in tp.notes} if tp else {}

            added_note_ids = sorted(set(t_notes.keys()) - set(b_notes.keys()))
            deleted_note_ids = sorted(set(b_notes.keys()) - set(t_notes.keys()))
            common_ids = set(b_notes.keys()) & set(t_notes.keys())
            modified_note_ids = [
                nid
                for nid in sorted(common_ids)
                if b_notes[nid].model_dump() != t_notes[nid].model_dump()
            ]

            part_diffs[pid] = {
                "base_note_count": len(b_notes),
                "target_note_count": len(t_notes),
                "added_notes": len(added_note_ids),
                "deleted_notes": len(deleted_note_ids),
                "modified_notes": len(modified_note_ids),
                "sample_added_ids": added_note_ids[:10],
                "sample_deleted_ids": deleted_note_ids[:10],
                "sample_modified_ids": modified_note_ids[:10],
            }

        return {
            "project_id": project_id,
            "revision_id": revision_id,
            "base_revision_id": base_revision_id,
            "parts": part_diffs,
        }
