"""L2 Coding Agent の run ライフサイクル・ステージング領域管理サービス(#46, FR-23)。

エージェントの `score_apply_ops` はステージング領域(`score/staging/{run_id}.json`)に
適用され、ユーザーが承認するまで `current.json` は一切変更されない(§8.8)。
キャンセル時はステージング領域を破棄し、`current.json` は1バイトも変更しない(FR-23)。
上限到達時(`truncated`)はステージングされた変更を破棄せず保持し、部分成果として
ユーザーに提示する(§8.9)。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from app.agent.audit import AuditEntry, audit_log_path
from app.agent.provider import AgentRunNotFoundError, AgentRunStatus
from app.agent.workspace import (
    clean_workspace,
    read_report,
    setup_agent_workspace,
    write_report,
)
from app.domain.migrations import migrate_to_current
from app.domain.score import ScoreIR
from app.infra import db, storage
from app.pipeline.refine.l1_diff import (
    NoteDiffChange,
    apply_change_to_current,
    compute_diff,
    filter_by_scope,
    revert_change_in_staging,
)
from app.services import score_undo
from app.services.project_service import ProjectNotFoundError
from app.services.score_service import ScoreNotFoundError, ScoreService

_ACTOR_USER = "user"


class ScoreConcurrentModificationError(RuntimeError):
    """楽観的ロック違反: current.json が他プロセスやジョブにより並行更新された場合。"""


@dataclass
class AgentRunService:
    workspace_dir: Path

    def _conn(self) -> sqlite3.Connection:
        return db.get_connection(self.workspace_dir / "db.sqlite3")

    def create_run(
        self, *, run_id: str, project_id: str, status: AgentRunStatus = "running"
    ) -> dict[str, Any]:
        """エージェント run を SQLite に登録する。"""
        # プロジェクトの存在確認
        project_path = storage.project_dir(self.workspace_dir, project_id)
        if not project_path.exists():
            raise ProjectNotFoundError(f"project not found: {project_id!r}")

        now = datetime.now(UTC).isoformat()
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO agent_runs(id, project_id, status, created_at) VALUES (?, ?, ?, ?)",
                (run_id, project_id, status, now),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            # 外部キー違反(project_id が存在しない)または一意制約違反(run_id 重複)
            if "FOREIGN KEY" in str(exc).upper():
                raise ProjectNotFoundError(f"project not found: {project_id!r}") from exc
            raise ValueError(f"agent run already exists: {run_id!r}") from exc

        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        """run_id からエージェント run のメタデータを取得する。

        `usage_json`(#49: `AgentRunManager`が`provider.result()`を受けて書き込む)は
        呼び出し元がdictとして扱えるよう、ここでparseした`usage`キーへ変換して返す。
        """
        row = (
            self._conn()
            .execute(
                "SELECT id, project_id, status, turns, usage_json, staged_ops_count, error, "
                "created_at FROM agent_runs WHERE id = ?",
                (run_id,),
            )
            .fetchone()
        )
        if row is None:
            raise AgentRunNotFoundError(f"agent run not found: {run_id!r}")
        result = dict(row)
        usage_json = result.pop("usage_json")
        result["usage"] = json.loads(usage_json) if usage_json else None
        return result

    def update_status(
        self,
        run_id: str,
        status: AgentRunStatus,
        *,
        turns: int | None = None,
        usage: dict[str, int] | None = None,
        staged_ops_count: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """run_id のステータス(および#49: providerの実行結果)を更新する。

        `turns`/`usage`/`staged_ops_count`/`error`は`None`の場合そのカラムを
        更新しない(#46のaccept/reject/cancelは`status`のみ更新し、実行結果は
        AgentRunManagerが`_drive_run`終了時にまとめて書き込むため)。

        終了ステータス(completed/failed/truncated/cancelled)確定時は
        保持/削除ポリシーを適用し、一時領域(scratch/)を自動クリーンアップする(#47)。
        """
        set_clauses = ["status = ?"]
        params: list[Any] = [status]
        if turns is not None:
            set_clauses.append("turns = ?")
            params.append(turns)
        if usage is not None:
            set_clauses.append("usage_json = ?")
            params.append(json.dumps(usage))
        if staged_ops_count is not None:
            set_clauses.append("staged_ops_count = ?")
            params.append(staged_ops_count)
        if error is not None:
            set_clauses.append("error = ?")
            params.append(error)
        params.append(run_id)

        conn = self._conn()
        cursor = conn.execute(
            f"UPDATE agent_runs SET {', '.join(set_clauses)} WHERE id = ?",  # noqa: S608
            params,
        )
        if cursor.rowcount == 0:
            raise AgentRunNotFoundError(f"agent run not found: {run_id!r}")
        conn.commit()

        run = self.get_run(run_id)
        if status in ("completed", "failed", "truncated", "cancelled"):
            workspace = storage.agent_workspace_dir(self.workspace_dir, run["project_id"], run_id)
            clean_workspace(workspace, keep_artifacts=True)

        return run

    def list_runs(self, project_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._conn()
        columns = "id, project_id, status, turns, usage_json, staged_ops_count, error, created_at"
        if project_id is not None:
            rows = conn.execute(
                f"SELECT {columns} FROM agent_runs WHERE project_id = ? ORDER BY created_at DESC",  # noqa: S608
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {columns} FROM agent_runs ORDER BY created_at DESC"  # noqa: S608
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            usage_json = item.pop("usage_json")
            item["usage"] = json.loads(usage_json) if usage_json else None
            results.append(item)
        return results

    def get_audit_log(self, run_id: str) -> list[AuditEntry]:
        """#49: run_id の監査ログ(`audit.jsonl`)を読み込む(FR-22)。

        まだ1件もツール呼び出しが無い(ファイル未作成)場合は空リストを返す
        (`get_report`と異なり、report.mdのような「完了時の必須成果物」ではなく
        run進行中から逐次読めるものであるため404にはしない)。壊れた行
        (不正なJSON/スキーマ不一致)は`score_note_history`(tools.py)と同じ
        「環境側の1件の破損で全体を失わない」方針でスキップする。
        """
        run = self.get_run(run_id)
        workspace = storage.agent_workspace_dir(self.workspace_dir, run["project_id"], run_id)
        log_path = audit_log_path(workspace)
        if not log_path.exists():
            return []
        entries: list[AuditEntry] = []
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(AuditEntry.model_validate_json(line))
                except ValidationError:
                    continue
        return entries

    def _read_current(self, project_id: str) -> tuple[dict[str, Any], ScoreIR]:
        score_path = storage.score_current_path(self.workspace_dir, project_id)
        if not score_path.exists():
            raise ScoreNotFoundError(f"score not found for project: {project_id!r}")
        raw = storage.read_json(score_path)
        return raw, ScoreIR.model_validate(migrate_to_current(raw))

    def _read_staged_optional(self, project_id: str, run_id: str) -> ScoreIR | None:
        path = storage.score_staging_path(self.workspace_dir, project_id, run_id)
        if not path.exists():
            return None
        raw = storage.read_json(path)
        return ScoreIR.model_validate(migrate_to_current(raw))

    def _read_undo_state_raw(self, project_id: str) -> dict[str, Any] | None:
        path = storage.score_undo_state_path(self.workspace_dir, project_id)
        if not path.exists():
            return None
        try:
            return storage.read_json(path)
        except (OSError, ValueError):
            return None

    def _record_ai_decision(
        self,
        project_id: str,
        *,
        op: Literal["ai.accept", "ai.reject", "ai.cancel"],
        run_id: str,
        part_id: str | None = None,
        bar_range: list[int] | None = None,
        changes: dict[str, score_undo.NoteChange],
    ) -> None:
        scope_dict = {
            "part": part_id,
            "bars": bar_range,
        }
        entry = score_undo.UndoEntry(
            ops=[{"op": op, "run_id": run_id, "scope": scope_dict}],
            actor=_ACTOR_USER,
            ts=datetime.now(UTC).isoformat(),
            changes=changes,
        )
        try:
            storage.append_jsonl(
                storage.score_ops_log_path(self.workspace_dir, project_id),
                entry.model_dump(mode="json"),
            )
            if op == "ai.accept" and changes:
                undo_state_path = storage.score_undo_state_path(self.workspace_dir, project_id)
                state = score_undo.read_undo_state(undo_state_path)
                score_undo.record_edit(state, entry)
                score_undo.write_undo_state(undo_state_path, state)
        except (OSError, ValueError) as exc:
            print(
                f"[agent_run_service] warning: failed to persist ai decision log "
                f"for project {project_id}: {exc}",
                file=sys.stderr,
            )

    def get_diff(self, run_id: str) -> tuple[str, list[NoteDiffChange]]:
        """ステージング vs current の差分を取得する(§8.8)。

        ステージングファイルがまだ作成されていない(score_apply_ops 未実行)
        場合は、差分なし(空リスト)を返す。
        """
        run = self.get_run(run_id)
        project_id = run["project_id"]
        _, current = self._read_current(project_id)
        staged = self._read_staged_optional(project_id, run_id)
        if staged is None:
            return project_id, []
        return project_id, compute_diff(current, staged, run_id)

    def _verify_optimistic_lock(
        self,
        project_id: str,
        raw_before: dict[str, Any],
        raw_undo_before: dict[str, Any] | None,
    ) -> None:
        """楽観的ロックの検証: current.json または undo 状態が並行変更されていれば

        例外を送出する。
        """
        score_path = storage.score_current_path(self.workspace_dir, project_id)
        raw_now = storage.read_json(score_path) if score_path.exists() else None
        if raw_now != raw_before or self._read_undo_state_raw(project_id) != raw_undo_before:
            raise ScoreConcurrentModificationError(
                "score/current.json was modified concurrently; please retry with the latest score"
            )

    def accept(
        self,
        run_id: str,
        *,
        part_id: str | None = None,
        bar_range: list[int] | None = None,
    ) -> tuple[dict[str, Any], list[int], list[NoteDiffChange]]:
        """ステージングされた差分を current.json へ確定適用する(スコープ指定対応)。

        全差分が確定された場合(remaining が空)、run の status を completed へ遷移させる。
        """
        run = self.get_run(run_id)
        project_id = run["project_id"]

        raw_before, current = self._read_current(project_id)
        raw_undo_before = self._read_undo_state_raw(project_id)
        staged = self._read_staged_optional(project_id, run_id)

        if staged is None:
            # ステージングが存在しない場合は差分なし
            return current.model_dump(mode="json"), [], []

        target = filter_by_scope(
            compute_diff(current, staged, run_id), part_id=part_id, bar_range=bar_range
        )

        before_snapshot = score_undo.snapshot_notes(current)
        applied_note_ids: list[int] = []
        for change in target:
            apply_change_to_current(change, current=current, staged=staged)
            applied_note_ids.extend(change.note_ids)
        after_snapshot = score_undo.snapshot_notes(current)
        changes_snapshot = score_undo.diff_snapshots(before_snapshot, after_snapshot)

        # 楽観的ロック検証(#46 Gate2レビュー指摘・1巡目: 共通ヘルパで検証)
        self._verify_optimistic_lock(project_id, raw_before, raw_undo_before)

        if changes_snapshot:
            ScoreService(workspace_dir=self.workspace_dir).write_score(project_id, current)
            self._record_ai_decision(
                project_id,
                op="ai.accept",
                run_id=run_id,
                part_id=part_id,
                bar_range=bar_range,
                changes=changes_snapshot,
            )

        remaining = compute_diff(current, staged, run_id)
        # 全差分が適用完了した場合は completed へ遷移(#46 Gate2レビュー指摘・1巡目)
        if not remaining and run["status"] != "completed":
            self.update_status(run_id, "completed")

        return current.model_dump(mode="json"), applied_note_ids, remaining

    def reject(
        self,
        run_id: str,
        *,
        part_id: str | None = None,
        bar_range: list[int] | None = None,
    ) -> tuple[list[int], list[NoteDiffChange]]:
        """スコープ内のステージング変更を却下する。current.json は変更しない。

        accept と同様に楽観的ロックを検証し、全変更が却下された場合は completed へ遷移する。
        """
        run = self.get_run(run_id)
        project_id = run["project_id"]

        raw_before, current = self._read_current(project_id)
        raw_undo_before = self._read_undo_state_raw(project_id)
        staged = self._read_staged_optional(project_id, run_id)

        if staged is None:
            return [], []

        target = filter_by_scope(
            compute_diff(current, staged, run_id), part_id=part_id, bar_range=bar_range
        )
        reverted_note_ids: list[int] = []
        for change in target:
            revert_change_in_staging(change, current=current, staged=staged)
            reverted_note_ids.extend(change.note_ids)

        if target:
            # 楽観的ロック検証(#46 Gate2レビュー指摘・1巡目: reject時も並行更新を検知)
            self._verify_optimistic_lock(project_id, raw_before, raw_undo_before)
            staging_path = storage.score_staging_path(self.workspace_dir, project_id, run_id)
            storage.write_json(staging_path, staged.model_dump(mode="json"))
            self._record_ai_decision(
                project_id,
                op="ai.reject",
                run_id=run_id,
                part_id=part_id,
                bar_range=bar_range,
                changes={},
            )

        remaining = compute_diff(current, staged, run_id)
        # 全差分が却下完了した場合も completed へ遷移(#46 Gate2レビュー指摘・1巡目)
        if not remaining and run["status"] != "completed":
            self.update_status(run_id, "completed")

        return reverted_note_ids, remaining

    def cancel(self, run_id: str) -> dict[str, Any]:
        """エージェント run をキャンセルし、ステージング領域を破棄する(FR-23)。

        重要: 実行途中でキャンセルしても current.json は1バイトも変更されない。
        既に completed の run は承認履歴との整合性を守るためキャンセルを拒否する。
        """
        run = self.get_run(run_id)
        project_id = run["project_id"]

        if run["status"] == "completed":
            raise ValueError(f"cannot cancel an already completed agent run: {run_id!r}")
        if run["status"] == "cancelled":
            return {
                "run_id": run_id,
                "status": "cancelled",
                "project_id": project_id,
            }

        # ステージングファイルを破棄(存在する場合)
        staging_path = storage.score_staging_path(self.workspace_dir, project_id, run_id)
        staging_path.unlink(missing_ok=True)

        # ステータスを cancelled に更新(update_status内で保持/削除ポリシー適用)
        self.update_status(run_id, "cancelled")

        # 監査ログにキャンセルを記録
        self._record_ai_decision(
            project_id,
            op="ai.cancel",
            run_id=run_id,
            changes={},
        )

        return {
            "run_id": run_id,
            "status": "cancelled",
            "project_id": project_id,
        }

    def create_workspace(
        self,
        run_id: str,
        *,
        task_type: str,
        prompt: str,
        scope: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> Path:
        """#47: run_id に対応するエージェントワークスペースを構築する(設計書§8.6)。"""
        run = self.get_run(run_id)
        project_id = run["project_id"]
        score = ScoreService(self.workspace_dir).read_score(project_id)
        workspace = storage.agent_workspace_dir(self.workspace_dir, project_id, run_id)
        return setup_agent_workspace(
            workspace,
            task_type=task_type,
            project_id=project_id,
            run_id=run_id,
            prompt=prompt,
            score=score,
            scope=scope,
            allowed_tools=allowed_tools,
            model=model,
            max_turns=max_turns,
        )

    def get_report(self, run_id: str) -> str:
        """#47: run_id のワークスペースから report.md を取得する(設計書§8.6, §11.3)。"""
        run = self.get_run(run_id)
        project_id = run["project_id"]
        workspace = storage.agent_workspace_dir(self.workspace_dir, project_id, run_id)
        return read_report(workspace)

    def write_report(self, run_id: str, content: str) -> Path:
        """#47: run_id のワークスペースに report.md を書き込む。"""
        run = self.get_run(run_id)
        project_id = run["project_id"]
        workspace = storage.agent_workspace_dir(self.workspace_dir, project_id, run_id)
        return write_report(workspace, content)
