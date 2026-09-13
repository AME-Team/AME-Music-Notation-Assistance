"""Stage 6手前: L1構造化出力による注釈API(#39, 設計書§7.3/§11.3)。

`POST /refine`は`current.json`を一切書き換えない。L1の提案は
`score/staging/{run_id}.json`へ保存し、ユーザーがDiffPanel(#41、未実装)で
採否を決めて初めて`current.json`に反映される設計とする(設計書§10.3、M4完了
条件「L0とL1の差分を確認し小節単位で採否を決める」)。

`mode: "batch"`は#40の担当のためこのIssueでは未対応: `mode`は`Literal["sync"]`
のみを許可し、未対応の値(将来の`"batch"`含む)は型検証で422として拒否する。
未知フィールド全般は`RefineRequest`の`ConfigDict(extra="forbid")`で拒否する
(#39 Gate2レビュー指摘: 以前はこのdocstringが`extra="forbid"`を使わないと
誤って説明しており、実装と矛盾していた)。
"""

from __future__ import annotations

from typing import Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.deps import (
    ensure_project_exists,
    get_project_service,
    get_settings,
    read_score_or_404,
)
from app.config import Settings, resolve_api_key
from app.infra import ids, storage
from app.pipeline.quantize import beat_tick_anchors
from app.pipeline.refine.l1_client import DEFAULT_EFFORT, DEFAULT_MODEL
from app.pipeline.refine.l1_runner import run_l1_sequential
from app.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["refine"])


class RefineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["sync"]
    part_id: str
    model: str = DEFAULT_MODEL
    effort: Literal["high", "medium"] = DEFAULT_EFFORT


class RefineResponse(BaseModel):
    run_id: str
    chunks_ok: int
    chunks_rejected: int
    rejected_reasons: list[str]
    skipped_decisions: list[str]
    usage: dict[str, int]


def _read_beat_anchors_or_422(
    project_id: str, settings: Settings, divisions: int
) -> list[tuple[float, float]]:
    """`api/score.py`の`_apply_score_ops`と同じ理由でフェイルクローズする

    (#31-M3レビュー指摘と同じ設計判断: beatmap未実行/欠損時に無言でtick→秒
    変換を続けると`onset_sec`が誤った値になる)。
    """
    beatmap_file = storage.beatmap_path(settings.workspace_dir, project_id)
    if not beatmap_file.exists():
        raise HTTPException(status_code=404, detail="beatmap not found; run the beat stage first")
    beatmap = storage.read_json(beatmap_file)
    anchors = beat_tick_anchors(
        beatmap.get("beats", []), beatmap.get("time_signatures", []), divisions
    )
    if not anchors:
        raise HTTPException(
            status_code=422,
            detail="beatmap has no beats; cannot convert tick positions to seconds",
        )
    return anchors


@router.post("/refine", response_model=RefineResponse)
def refine_score(
    project_id: str,
    body: RefineRequest,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> RefineResponse:
    """量子化(#25)+L0(#26)実行済みが前提。未実行なら404を返す。

    `async def`にしない(`api/export.py`と同じ理由): `anthropic`のPython SDKは
    同期クライアントであり、`async def`のままだと待機中にイベントループを
    直接ブロックし、SSEでのジョブ進捗配信など他の同時リクエストを止めてしまう。
    """
    ensure_project_exists(project_id, service)
    score = read_score_or_404(project_id, settings)

    api_key = resolve_api_key("anthropic")
    if api_key is None:
        # NFR-12: AIが使えない状況でも成果物(L0まで)は必ず出る。L1が使えない
        # ことはL0の結果自体を無効化しない、という設計意図をメッセージにも反映する。
        raise HTTPException(
            status_code=503,
            detail="Anthropic API key is not configured; L0 results remain available",
        )

    beat_anchors = _read_beat_anchors_or_422(project_id, settings, score.divisions)

    client = anthropic.Anthropic(api_key=api_key)
    run_id = ids.new_id("run")

    try:
        result = run_l1_sequential(
            score,
            body.part_id,
            client=client,
            run_id=run_id,
            beat_anchors=beat_anchors,
            model=body.model,
            effort=body.effort,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # `ensure_project_layout`(プロジェクト作成時)で"score/staging"ディレクトリは
    # 既に作成済みのため、ここで改めてmkdirする必要はない(`api/export.py`と同じ判断)。
    staging_path = storage.score_staging_path(settings.workspace_dir, project_id, run_id)
    storage.write_json(staging_path, result.staged_score.model_dump(mode="json"))

    return RefineResponse(
        run_id=run_id,
        chunks_ok=result.chunks_ok,
        chunks_rejected=result.chunks_rejected,
        rejected_reasons=result.rejected_reasons,
        skipped_decisions=result.skipped_decisions,
        usage=result.usage,
    )
