"""L0 vs L1提案の差分表示・承認/却下API(#41, 設計書§4.1 FR-10/§10.4/§11.3/§12.3)。

`GET .../diff`は`current.json`と`score/staging/{run_id}.json`を比較し、その
runが実際に変更したノートだけを一覧化する(`pipeline/refine/l1_diff.py`)。

設計書は`/api/agent/runs/{run_id}/diff`のようにproject_idを含まないパスを示すが、
L1のrunはSQLite等に登録されておらず(#39/#40はステージングファイルのみで完結)、
run_idからproject_idを逆引きする手段が無い。既存の`refine.py`と同じ
`/api/projects/{project_id}/...`規約に合わせ、run_idはこのプレフィックス配下の
パスパラメータとする(#41着手前にユーザーと合意済みの設計判断)。

承認(`POST .../accept`)は`current.json`を書き換える一括ノート編集操作のため、
既存のUndo/Redo基盤(`services/score_undo.py`、#32)にそのまま乗せる: 対象ノートの
スナップショット差分を取り、`actor="user"`のUndoEntryとして記録する(設計書§10.4の
`ai.accept`の`actor`はこの操作を行ったユーザーを指す)。これによりUndoボタンで
取り消せる(人の編集とAIの編集が同一機構に乗るという設計書の意図に合致)。

却下(`POST .../reject`)は`current.json`を変更しない。再度差分取得した際に却下済みの
変更が「未処理」として再表示されないよう、`score/staging/{run_id}.json`側の該当
ノートをcurrent相当の値に書き戻す(#41着手前にユーザーと合意済み)。current側の変更が
無いためUndo/Redoスタックには積まず、監査ログ(`score/ops.jsonl`)にのみ`ai.reject`を
追記する。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ensure_project_exists, get_project_service, get_settings
from app.config import Settings
from app.domain.migrations import migrate_to_current
from app.domain.score import ScoreIR
from app.infra import storage
from app.pipeline.refine.l1_diff import (
    NoteDiffChange,
    apply_change_to_current,
    compute_diff,
    filter_by_scope,
    revert_change_in_staging,
)
from app.services import score_undo
from app.services.project_service import ProjectService
from app.services.score_service import ScoreService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["diff"])

_ACTOR_USER = "user"


class NoteChangeResponse(BaseModel):
    change_type: Literal["keep", "delete", "split_tie"]
    part_id: str
    bar: int
    note_ids: list[int]
    changed_fields: list[str]
    midi: int
    before: dict[str, Any]
    after: dict[str, Any]
    ai_reason: str | None


class DiffResponse(BaseModel):
    run_id: str
    changes: list[NoteChangeResponse]


class ScopeRequest(BaseModel):
    """`accept`/`reject`共通のスコープ指定。両方省略時はrun全体が対象(#41)。"""

    model_config = ConfigDict(extra="forbid")

    part_id: str | None = None
    bar_range: list[int] | None = Field(default=None, min_length=2, max_length=2)


class AcceptResponse(BaseModel):
    score: dict[str, Any]
    applied_note_ids: list[int]
    remaining_diff: DiffResponse


class RejectResponse(BaseModel):
    reverted_note_ids: list[int]
    remaining_diff: DiffResponse


def to_diff_response(run_id: str, changes: list[NoteDiffChange]) -> DiffResponse:
    """NoteDiffChangeのリストをAPI共通のDiffResponseモデルへ変換する。"""
    return DiffResponse(
        run_id=run_id,
        changes=[
            NoteChangeResponse(
                change_type=c.change_type,
                part_id=c.part_id,
                bar=c.bar,
                note_ids=c.note_ids,
                changed_fields=c.changed_fields,
                midi=c.midi,
                before=c.before,
                after=c.after,
                ai_reason=c.ai_reason,
            )
            for c in changes
        ],
    )


# 既存コードとの後方互換用エイリアス
_to_response = to_diff_response


def _read_current(project_id: str, settings: Settings) -> tuple[dict, ScoreIR]:
    """`current.json`の生JSONとパース済み`ScoreIR`を返す。

    `api/score.py`の`_read_score_raw_or_404`と同じ理由(楽観的並行性制御用に
    生JSONも保持する)だが、`api/refine.py`の`_read_beat_anchors_or_422`と同様、
    モジュールを跨いだ再利用はせずこのファイル内に複製する(既存の慣例)。
    """
    path = storage.score_current_path(settings.workspace_dir, project_id)
    if not path.exists():
        raise HTTPException(
            status_code=404, detail="score not found; run the transcribe stage first"
        )
    raw = storage.read_json(path)
    return raw, ScoreIR.model_validate(migrate_to_current(raw))


def _read_staged(project_id: str, run_id: str, settings: Settings) -> ScoreIR:
    path = storage.score_staging_path(settings.workspace_dir, project_id, run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"refine run not found: {run_id!r}")
    raw = storage.read_json(path)
    return ScoreIR.model_validate(migrate_to_current(raw))


def _read_undo_state_raw(project_id: str, settings: Settings) -> dict | None:
    """`api/score.py`の同名関数と同じフォールバック方針(#32-M3レビュー指摘参照)。"""
    path = storage.score_undo_state_path(settings.workspace_dir, project_id)
    if not path.exists():
        return None
    try:
        return storage.read_json(path)
    except (OSError, ValueError):
        return None


def _record_ai_decision(
    workspace_dir: Path,
    project_id: str,
    *,
    op: Literal["ai.accept", "ai.reject"],
    run_id: str,
    scope: ScopeRequest,
    changes: dict[str, score_undo.NoteChange],
) -> None:
    """監査ログ(`score/ops.jsonl`)への追記(設計書§10.4)。

    `ai.accept`のみ実際にcurrentへ反映された`changes`を伴うため、`score_undo`の
    Undo/Redoスタックにも積む(取り消し可能にする)。`ai.reject`はcurrentに変更が
    無いため監査ログへの追記のみに留める。

    `api/score.py`の`_record_edit_or_warn`と同じ設計(#32): このAPIの主目的
    (current.json/stagingの書き換え)は既に完了しているため、監査ログの永続化
    失敗はこの操作全体を失敗として扱わない。

    レビュー指摘(#41 Gate2): `OSError`だけでなく`ValueError`も捕捉する
    (`_read_undo_state_raw`と同じ理由: `score_undo.read_undo_state`は内部で
    破損したJSON/スキーマ不一致を`ValueError`系として自己修復するが、将来の
    変更でここが変わった場合に備え、呼び出し側でも同じフォールバック方針を
    崩さないようにする)。
    """
    scope_dict = {
        "part": scope.part_id,
        "bars": scope.bar_range,
    }
    entry = score_undo.UndoEntry(
        ops=[{"op": op, "run_id": run_id, "scope": scope_dict}],
        actor=_ACTOR_USER,
        ts=datetime.now(UTC).isoformat(),
        changes=changes,
    )
    try:
        storage.append_jsonl(
            storage.score_ops_log_path(workspace_dir, project_id), entry.model_dump(mode="json")
        )
        if op == "ai.accept" and changes:
            undo_state_path = storage.score_undo_state_path(workspace_dir, project_id)
            state = score_undo.read_undo_state(undo_state_path)
            score_undo.record_edit(state, entry)
            score_undo.write_undo_state(undo_state_path, state)
    except (OSError, ValueError) as exc:
        print(
            f"[diff] warning: failed to persist ai decision log for project {project_id}: {exc}",
            file=sys.stderr,
        )


@router.get("/refine/runs/{run_id}/diff", response_model=DiffResponse)
def get_diff(
    project_id: str,
    run_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> DiffResponse:
    """#41: `run_id`が変更したノートの一覧(FR-10)。"""
    ensure_project_exists(project_id, service)
    _, current = _read_current(project_id, settings)
    staged = _read_staged(project_id, run_id, settings)
    return _to_response(run_id, compute_diff(current, staged, run_id))


@router.post("/refine/runs/{run_id}/accept", response_model=AcceptResponse)
def accept_diff(
    project_id: str,
    run_id: str,
    body: ScopeRequest,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> AcceptResponse:
    """#41: スコープ内の変更を`current.json`へ適用する(省略時は全体承認)。

    楽観的並行性制御(`api/score.py`の`apply_score_ops`と同じパターン):
    リクエスト開始時に読んだ生JSON(`raw_before`)を保持し、書き込み直前に
    再読込して一致するか確認する。
    """
    ensure_project_exists(project_id, service)
    raw_before, current = _read_current(project_id, settings)
    raw_undo_before = _read_undo_state_raw(project_id, settings)
    staged = _read_staged(project_id, run_id, settings)

    target = filter_by_scope(
        compute_diff(current, staged, run_id), part_id=body.part_id, bar_range=body.bar_range
    )

    before_snapshot = score_undo.snapshot_notes(current)
    applied_note_ids: list[int] = []
    for change in target:
        apply_change_to_current(change, current=current, staged=staged)
        applied_note_ids.extend(change.note_ids)
    after_snapshot = score_undo.snapshot_notes(current)
    changes_snapshot = score_undo.diff_snapshots(before_snapshot, after_snapshot)

    score_path = storage.score_current_path(settings.workspace_dir, project_id)
    raw_now = storage.read_json(score_path) if score_path.exists() else None
    if raw_now != raw_before or _read_undo_state_raw(project_id, settings) != raw_undo_before:
        raise HTTPException(
            status_code=409,
            detail="score/current.json was modified concurrently (e.g. by a running "
            "quantize job); please retry with the latest score",
        )

    if changes_snapshot:
        ScoreService(workspace_dir=settings.workspace_dir).write_score(project_id, current)
        _record_ai_decision(
            settings.workspace_dir,
            project_id,
            op="ai.accept",
            run_id=run_id,
            scope=body,
            changes=changes_snapshot,
        )

    remaining = compute_diff(current, staged, run_id)
    return AcceptResponse(
        score=current.model_dump(mode="json"),
        applied_note_ids=applied_note_ids,
        remaining_diff=_to_response(run_id, remaining),
    )


@router.post("/refine/runs/{run_id}/reject", response_model=RejectResponse)
def reject_diff(
    project_id: str,
    run_id: str,
    body: ScopeRequest,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> RejectResponse:
    """#41: スコープ内の変更を却下する(省略時は全体却下)。`current.json`は変更しない。"""
    ensure_project_exists(project_id, service)
    _, current = _read_current(project_id, settings)
    staged = _read_staged(project_id, run_id, settings)

    target = filter_by_scope(
        compute_diff(current, staged, run_id), part_id=body.part_id, bar_range=body.bar_range
    )
    reverted_note_ids: list[int] = []
    for change in target:
        revert_change_in_staging(change, current=current, staged=staged)
        reverted_note_ids.extend(change.note_ids)

    if target:
        staging_path = storage.score_staging_path(settings.workspace_dir, project_id, run_id)
        storage.write_json(staging_path, staged.model_dump(mode="json"))
        _record_ai_decision(
            settings.workspace_dir,
            project_id,
            op="ai.reject",
            run_id=run_id,
            scope=body,
            changes={},
        )

    remaining = compute_diff(current, staged, run_id)
    return RejectResponse(
        reverted_note_ids=reverted_note_ids, remaining_diff=_to_response(run_id, remaining)
    )
