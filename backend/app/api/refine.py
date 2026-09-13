"""Stage 6手前: L1構造化出力による注釈API(#39/#40, 設計書§7.3/§7.5/§11.3)。

`POST /refine`は`current.json`を一切書き換えない。L1の提案は
`score/staging/{run_id}.json`へ保存し、ユーザーがDiffPanel(#41、未実装)で
採否を決めて初めて`current.json`に反映される設計とする(設計書§10.3、M4完了
条件「L0とL1の差分を確認し小節単位で採否を決める」)。

`mode`は`Literal["sync", "batch"]`をサポート(#40):
- `"batch"`: Anthropic Messages Batches APIを使用(R-9: コスト50%割引、既定値)。
- `"sync"`: チャンクごとの逐次同期呼び出し。
- NFR-07: レスポンスにトークン使用量(`usage`)と実測コスト(`cost_usd`)を含める。
- `GET /refine/estimate`: 実行前の事前見積もりエンドポイントを提供(§7.5)。
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
from app.pipeline.refine.cost import calculate_usage_cost_usd, estimate_refine_cost
from app.pipeline.refine.l1_batch import run_l1_batch
from app.pipeline.refine.l1_chunker import build_chunks
from app.pipeline.refine.l1_client import DEFAULT_EFFORT, DEFAULT_MODEL, L1ClientError
from app.pipeline.refine.l1_runner import run_l1_sequential
from app.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["refine"])


class RefineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Gate2レビュー指摘(PR #102): HTTPリクエストの長時間ブロッキングを防ぐため
    # 既定値は同期呼び出し("sync")とし、クライアントが明示的にバッチ割引(R-9)を
    # 要求した場合のみ"batch"を実行する設計とする。
    mode: Literal["sync", "batch"] = "sync"
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
    cost_usd: float


class RefineEstimateResponse(BaseModel):
    part_id: str
    mode: Literal["sync", "batch"]
    model: str
    num_chunks: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cache_read_tokens: int
    estimated_cost_usd: float


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


@router.get("/refine/estimate", response_model=RefineEstimateResponse)
def estimate_refine(
    project_id: str,
    part_id: str,
    mode: Literal["sync", "batch"] = "batch",
    model: str = DEFAULT_MODEL,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> RefineEstimateResponse:
    """実行前の想定コスト事前見積もりエンドポイント(§7.5/NFR-07)。"""
    ensure_project_exists(project_id, service)
    score = read_score_or_404(project_id, settings)

    if score.find_part(part_id) is None:
        raise HTTPException(status_code=422, detail=f"part {part_id!r} not found in score")

    chunks = build_chunks(score, part_id)
    estimate = estimate_refine_cost(len(chunks), model=model, mode=mode)

    return RefineEstimateResponse(
        part_id=part_id,
        mode=mode,
        model=model,
        num_chunks=estimate.num_chunks,
        estimated_input_tokens=estimate.estimated_input_tokens,
        estimated_output_tokens=estimate.estimated_output_tokens,
        estimated_cache_read_tokens=estimate.estimated_cache_read_tokens,
        estimated_cost_usd=estimate.estimated_cost_usd,
    )


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
        if body.mode == "batch":
            result = run_l1_batch(
                score,
                body.part_id,
                client=client,
                run_id=run_id,
                beat_anchors=beat_anchors,
                model=body.model,
                effort=body.effort,
            )
        else:
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
    except L1ClientError as exc:
        raise HTTPException(status_code=502, detail=f"L1 refine execution failed: {exc}") from exc

    cost_usd = calculate_usage_cost_usd(result.usage, body.model, mode=body.mode)

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
        cost_usd=cost_usd,
    )
