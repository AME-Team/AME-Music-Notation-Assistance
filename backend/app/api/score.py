"""Score IR取得・編集API(#23/#31, 設計書§11.5)。

`GET /score/preview.musicxml`(部分小節プレビュー)はM2完了条件に含まれない
ため未実装(ユーザー確認済み、将来PRで対応)。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import ensure_project_exists, get_project_service, get_settings, read_score_or_404
from app.api.schemas import ErrorResponse, ScoreOpsRequest
from app.config import Settings
from app.domain.migrations import migrate_to_current
from app.domain.score import ScoreIR
from app.infra import storage
from app.pipeline.quantize import beat_tick_anchors
from app.services import score_undo
from app.services.project_service import ProjectService
from app.services.score_ops import ScoreOpError, apply_ops
from app.services.score_service import ScoreService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["score"])

# #32: このエンドポイントの唯一の呼び出し元は人間向けフロントエンドUIのため、
# actorは常に"user"(L1/L2 AI整音・エージェントランタイムはM4/M5未実装のため
# "llm"/"agent:run_id"は現状発生しない、UndoEntry.actorのdocstring参照)。
_ACTOR_USER = "user"


def _read_score_raw_or_404(project_id: str, settings: Settings) -> tuple[dict, ScoreIR]:
    """`score/current.json`の生JSONとパース済み`ScoreIR`を返す(#31/#32)。

    `read_score_or_404`(`api/deps.py`、単に読んでパースするだけ)を使わないのは、
    `apply_score_ops`/`_undo_or_redo`の楽観的並行性制御が「リクエスト開始時に
    読んだ生JSON」を書き込み直前の再読込と比較する必要があるため
    (`run_quantize_stage`の2巡目レビュー指摘と同じ理由: raw_before用の読み取りと
    モデル構築に使う読み取りを分離すると、その間の並行書き込みを検出できず
    lost-updateになる)。
    """
    score_path = storage.score_current_path(settings.workspace_dir, project_id)
    if not score_path.exists():
        raise HTTPException(
            status_code=404, detail="score not found; run the transcribe stage first"
        )
    raw = storage.read_json(score_path)
    return raw, ScoreIR.model_validate(migrate_to_current(raw))


def _read_undo_state_raw(project_id: str, settings: Settings) -> dict | None:
    """`undo_state.json`の生JSON(無い/読み取れなければ`None`)。#32-M3レビュー指摘対応:

    スコア本体と同じ楽観的並行性制御をこのファイルにも及ぼすための読み取り。

    #32-M3レビュー指摘(2巡目、HIGH): 破損した`undo_state.json`
    (不正なJSON/クラッシュ時の部分書き込み等)に対して`storage.read_json`が
    `json.JSONDecodeError`を送出すると、`score_undo.read_undo_state`が
    まさにこの状況を握りつぶして空状態へフォールバックする設計(#32)に
    反して、この生読み取りだけが例外を伝播させ`/score/ops`等を丸ごと500に
    してしまう。`read_undo_state`と同じ`except (OSError, ValueError)`で
    フォールバックする(`None`は「ファイルが無い」と同じ扱いになるため、
    後続の比較は「変化した」側に倒れ、安全側の409またはself-healing
    (次の書き込みで上書きされる)につながる)。
    """
    path = storage.score_undo_state_path(settings.workspace_dir, project_id)
    if not path.exists():
        return None
    try:
        return storage.read_json(path)
    except (OSError, ValueError) as exc:
        print(
            f"[score] warning: undo_state.json is unreadable for project {project_id}: {exc}",
            file=sys.stderr,
        )
        return None


def _record_edit_or_warn(
    workspace_dir: Path,
    project_id: str,
    *,
    ops_raw: list[dict],
    changes: dict[str, score_undo.NoteChange],
) -> None:
    """`ops.jsonl`への追記+`undo_state.json`の更新(#32)。

    スコア本体(`score/current.json`)の書き込みより優先度の低い副次的な永続化と
    位置づける: 失敗してもこのエディット自体は成功として扱い(スコア本体は
    正しく永続化済みのため)、`OSError`のみを捕捉してstderrへ警告するに留める
    (広範な`except Exception`は使わない、AME-AI-Review-Systemのsemgrepカスタム
    ルールでbroad exception catchが禁止されているため)。
    """
    if not changes:
        return
    entry = score_undo.UndoEntry(
        ops=ops_raw,
        actor=_ACTOR_USER,
        ts=datetime.now(UTC).isoformat(),
        changes=changes,
    )
    try:
        storage.append_jsonl(
            storage.score_ops_log_path(workspace_dir, project_id),
            entry.model_dump(mode="json"),
        )
        undo_state_path = storage.score_undo_state_path(workspace_dir, project_id)
        state = score_undo.read_undo_state(undo_state_path)
        score_undo.record_edit(state, entry)
        score_undo.write_undo_state(undo_state_path, state)
    except OSError as exc:
        print(
            f"[score] warning: failed to persist undo history for project {project_id}: {exc}",
            file=sys.stderr,
        )


@router.get("/score")
def get_score(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Stage 3(採譜)未実行なら404を返す。

    `async def` ではなく通常の `def` にする(`api/export.py`の`export_score`と
    同じ理由、#27-M2レビュー指摘): `ScoreService.read_score`はファイルI/Oを
    伴う同期処理であり、`async def`のままだとイベントループを直接ブロックし、
    SSEでのジョブ進捗配信など他の同時リクエストを止めてしまう。

    `response_model`に`domain.score.ScoreIR`を直接指定しない: そのネストする
    `TempoMapEntry`/`TimeSignatureEntry`が`api/schemas.py`(beatmap用)の同名
    クラスとOpenAPIコンポーネント名で衝突し、生成TSの型名が
    `app__api__schemas__TempoMapEntry`等へ改名されてしまう(#23-M2レビュー
    指摘)。M2のフロントエンドはこのエンドポイントの型付けを必要としない
    (設計書§11.5の完了条件外)ため、素の`dict`で返し衝突を避ける。
    """
    ensure_project_exists(project_id, service)
    score = read_score_or_404(project_id, settings)
    return score.model_dump(mode="json")


@router.post(
    "/score/ops",
    responses={
        404: {"model": ErrorResponse, "description": "score not found"},
        409: {"model": ErrorResponse, "description": "score modified concurrently"},
        # 422はFastAPIのリクエストボディ検証エラー(detailが配列)とScoreOpError
        # (detailが文字列)の両方で使われ、単一のresponses契約で正しく表現できない
        # ため宣言しない(export_score/patch_beatmapも同じ理由で宣言していない、
        # #31-M3レビュー指摘)。
    },
)
def apply_score_ops(
    project_id: str,
    body: ScoreOpsRequest,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#31: ノート編集オペレーションを配列で一括適用する(FR-09)。

    `async def` ではなく通常の `def` にする(`get_score`/`export_score`と同じ
    理由): ファイルI/Oが中心の同期処理。

    楽観的並行性制御(`worker/dsp_main.py`の各ステージ、`patch_beatmap`と同じ
    パターン): リクエスト開始時に読んだ生JSON(`raw_before`)を保持し、
    `apply_ops`はその場でパースした`ScoreIR`(まだ書き込んでいない)に対して
    行う。書き込み直前に再読込して`raw_before`と一致するか確認し、不一致なら
    409(`_read_score_raw_or_404`のdocstring参照)。

    #32-M3レビュー指摘: `score/current.json`だけでなく`score/undo_state.json`も
    同じ比較対象に含める。このエンドポイントは`_record_edit_or_warn`経由で
    undo_state.jsonも更新するため、対象に含めないと「本リクエストが
    score/ops.jsonlへ追記→undo_state.jsonを更新するまでの間に、別リクエスト
    (`/score/undo`等)がundo_state.jsonを読んで書き換える」という競合を
    見逃し、どちらか一方の更新が消える(lost update)。ただし
    `_record_edit_or_warn`自身がこのチェックの後でundo_state.jsonを再度
    読み直すため、そこから実際に書き込むまでの狭い窓は依然として残る
    (`patch_beatmap`等で既に許容されているのと同種の、単一ユーザー向け
    デスクトップアプリとして許容するTOCTOUギャップ)。
    """
    ensure_project_exists(project_id, service)
    raw_before, score = _read_score_raw_or_404(project_id, settings)
    raw_undo_before = _read_undo_state_raw(project_id, settings)
    score_path = storage.score_current_path(settings.workspace_dir, project_id)

    # beatmap未実行での編集は通常あり得ない(編集対象のnote.onset_tick自体が
    # quantize済み=beatmap実行済みの結果でしか存在しないはず)。それでも
    # beatmapが欠落/破損している状態でtick→秒変換を続けると、`anchors=[]`
    # により全tickが無言でtime=0.0扱いになり、onset_sec/duration_secが
    # 誤った値のまま永続化されてしまう(#31-M3レビュー指摘)。無言の
    # フォールバックより明示的なエラーの方が安全なため、フェイルクローズする。
    beatmap_file = storage.beatmap_path(settings.workspace_dir, project_id)
    if not beatmap_file.exists():
        raise HTTPException(status_code=404, detail="beatmap not found; run the beat stage first")
    beatmap = storage.read_json(beatmap_file)
    anchors = beat_tick_anchors(
        beatmap.get("beats", []), beatmap.get("time_signatures", []), score.divisions
    )
    if not anchors:
        raise HTTPException(
            status_code=422,
            detail="beatmap has no beats; cannot convert tick positions to seconds",
        )

    # #32: Undo/Redo用に、適用前後の全ノートをスナップショットして差分を取る
    # (op種別ごとの逆操作を手書きしない設計、`services/score_undo.py`参照)。
    before_snapshot = score_undo.snapshot_notes(score)

    try:
        apply_ops(score, body.ops, anchors)
    except ScoreOpError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    after_snapshot = score_undo.snapshot_notes(score)
    changes = score_undo.diff_snapshots(before_snapshot, after_snapshot)

    raw_now = storage.read_json(score_path) if score_path.exists() else None
    if raw_now != raw_before or _read_undo_state_raw(project_id, settings) != raw_undo_before:
        raise HTTPException(
            status_code=409,
            detail="score/current.json was modified concurrently (e.g. by a running "
            "quantize job); please retry with the latest score",
        )

    ScoreService(workspace_dir=settings.workspace_dir).write_score(project_id, score)
    _record_edit_or_warn(
        settings.workspace_dir,
        project_id,
        ops_raw=[op.model_dump(mode="json") for op in body.ops],
        changes=changes,
    )
    return score.model_dump(mode="json")


def _undo_or_redo(
    project_id: str,
    service: ProjectService,
    settings: Settings,
    *,
    direction: str,
) -> dict:
    """`/score/undo`・`/score/redo`共通のロジック(#32)。

    既存の`apply_score_ops`と同じ楽観的並行性制御パターン(`raw_before`保持→
    適用→`raw_now`再読込比較→409)を、`score/current.json`と
    `score/undo_state.json`の両方に対して行う(`apply_score_ops`のdocstring
    参照、#32-M3レビュー指摘)。
    """
    ensure_project_exists(project_id, service)
    raw_before, score = _read_score_raw_or_404(project_id, settings)
    score_path = storage.score_current_path(settings.workspace_dir, project_id)
    raw_undo_before = _read_undo_state_raw(project_id, settings)

    undo_state_path = storage.score_undo_state_path(settings.workspace_dir, project_id)
    state = score_undo.read_undo_state(undo_state_path)
    applied = (
        score_undo.apply_undo(score, state)
        if direction == "undo"
        else score_undo.apply_redo(score, state)
    )

    if applied:
        raw_now = storage.read_json(score_path) if score_path.exists() else None
        if raw_now != raw_before or _read_undo_state_raw(project_id, settings) != raw_undo_before:
            raise HTTPException(
                status_code=409,
                detail="score/current.json was modified concurrently (e.g. by a running "
                "quantize job); please retry with the latest score",
            )
        ScoreService(workspace_dir=settings.workspace_dir).write_score(project_id, score)
        try:
            score_undo.write_undo_state(undo_state_path, state)
        except OSError as exc:
            print(
                f"[score] warning: failed to persist undo state for project {project_id}: {exc}",
                file=sys.stderr,
            )

    return {"score": score.model_dump(mode="json"), "applied": applied}


@router.post(
    "/score/undo",
    responses={
        404: {"model": ErrorResponse, "description": "score not found"},
        409: {"model": ErrorResponse, "description": "score modified concurrently"},
    },
)
def undo_score_ops(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#32: 直近の編集を1つ元に戻す。Undoスタックが空なら`applied=false`で200を返す

    (「元に戻す操作が無い」はエラーではないため)。
    """
    return _undo_or_redo(project_id, service, settings, direction="undo")


@router.post(
    "/score/redo",
    responses={
        404: {"model": ErrorResponse, "description": "score not found"},
        409: {"model": ErrorResponse, "description": "score modified concurrently"},
    },
)
def redo_score_ops(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#32: 直近にUndoした編集を1つやり直す。Redoスタックが空なら`applied=false`で

    200を返す。
    """
    return _undo_or_redo(project_id, service, settings, direction="redo")
