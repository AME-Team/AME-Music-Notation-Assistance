"""Score IR取得・編集API(#23/#31, 設計書§11.5)。

`GET /score/preview.musicxml`(部分小節プレビュー)はM2完了条件に含まれない
ため未実装(ユーザー確認済み、将来PRで対応)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import ensure_project_exists, get_project_service, get_settings, read_score_or_404
from app.api.schemas import ErrorResponse, ScoreOpsRequest
from app.config import Settings
from app.domain.migrations import migrate_to_current
from app.domain.score import ScoreIR
from app.infra import storage
from app.pipeline.quantize import beat_tick_anchors
from app.services.project_service import ProjectService
from app.services.score_ops import ScoreOpError, apply_ops
from app.services.score_service import ScoreService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["score"])


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
    409。`read_score_or_404`(単に読んでパースするだけ)ではなく生JSONを直接
    読むのは、`run_quantize_stage`の2巡目レビュー指摘と同じ理由: `raw_before`用
    の読み取りと、モデル構築に使う読み取りを分離すると、その間の並行書き込みを
    検出できずlost-updateになる。
    """
    ensure_project_exists(project_id, service)
    score_path = storage.score_current_path(settings.workspace_dir, project_id)
    if not score_path.exists():
        raise HTTPException(
            status_code=404, detail="score not found; run the transcribe stage first"
        )
    raw_before = storage.read_json(score_path)
    score = ScoreIR.model_validate(migrate_to_current(raw_before))

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

    try:
        apply_ops(score, body.ops, anchors)
    except ScoreOpError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    raw_now = storage.read_json(score_path) if score_path.exists() else None
    if raw_now != raw_before:
        raise HTTPException(
            status_code=409,
            detail="score/current.json was modified concurrently (e.g. by a running "
            "quantize job); please retry with the latest score",
        )

    ScoreService(workspace_dir=settings.workspace_dir).write_score(project_id, score)
    return score.model_dump(mode="json")
