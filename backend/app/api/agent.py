"""L2 Coding Agent API(#42, #46, #47, 設計書§11.3)。

- `GET /api/agent/providers`: プロバイダ一覧(#42)
- `GET /api/agent/runs/{run_id}/diff`: ステージング vs current の差分(#46)
- `POST /api/agent/runs/{run_id}/accept`: 差分の承認(スコープ指定対応)(#46)
- `POST /api/agent/runs/{run_id}/reject`: 差分の却下(スコープ指定対応)(#46)
- `POST /api/agent/runs/{run_id}/cancel`: キャンセル(ステージング破棄・current無変更)(#46)
- `GET /api/agent/runs/{run_id}/report`: 成果報告 report.md の取得(#47)
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agent.provider import AgentRunNotFoundError
from app.agent.providers.dummy import DummyAgentProvider
from app.agent.workspace import ReportNotFoundError
from app.api.deps import get_agent_run_service
from app.api.diff import (
    AcceptResponse,
    DiffResponse,
    RejectResponse,
    ScopeRequest,
    to_diff_response,
)
from app.services.agent_run_service import (
    AgentRunService,
    ScoreConcurrentModificationError,
)
from app.services.score_service import ScoreNotFoundError

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentProviderInfo(BaseModel):
    name: str
    configured: bool


class CancelResponse(BaseModel):
    run_id: str
    status: Literal["cancelled"]
    project_id: str


class AgentReportResponse(BaseModel):
    run_id: str
    content: str


@router.get("/providers", response_model=list[AgentProviderInfo])
def list_providers() -> list[AgentProviderInfo]:
    """#48/#52でプロバイダが増えるたびにここへ追記する想定の静的レジストリ。

    `name`は各プロバイダクラスの`name`属性を参照する(#42 Gate2レビュー指摘・
    2巡目 LOW: 文字列リテラルをここへ直書きすると`DummyAgentProvider.name`との
    二重管理になり、片方だけ更新漏れが起こりうる)。`dummy`は外部依存が無く
    常に実行可能なため`configured=True`固定。
    """
    return [AgentProviderInfo(name=DummyAgentProvider.name, configured=True)]


@router.get("/runs/{run_id}/diff", response_model=DiffResponse)
def get_agent_diff(
    run_id: str,
    service: AgentRunService = Depends(get_agent_run_service),
) -> DiffResponse:
    """#46: エージェント run のステージング変更 vs current.json の差分を取得する(FR-23)。"""
    try:
        _, changes = service.get_diff(run_id)
        return to_diff_response(run_id, changes)
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/accept", response_model=AcceptResponse)
def accept_agent_diff(
    run_id: str,
    body: ScopeRequest,
    service: AgentRunService = Depends(get_agent_run_service),
) -> AcceptResponse:
    """#46: ステージング変更を current.json へ確定適用する(スコープ指定可能、FR-23)。"""
    try:
        score_dict, applied_note_ids, remaining = service.accept(
            run_id, part_id=body.part_id, bar_range=body.bar_range
        )
        return AcceptResponse(
            score=score_dict,
            applied_note_ids=applied_note_ids,
            remaining_diff=to_diff_response(run_id, remaining),
        )
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScoreConcurrentModificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/reject", response_model=RejectResponse)
def reject_agent_diff(
    run_id: str,
    body: ScopeRequest,
    service: AgentRunService = Depends(get_agent_run_service),
) -> RejectResponse:
    """#46: ステージング変更を却下し元に戻す。current.json は変更しない(FR-23)。"""
    try:
        reverted_note_ids, remaining = service.reject(
            run_id, part_id=body.part_id, bar_range=body.bar_range
        )
        return RejectResponse(
            reverted_note_ids=reverted_note_ids,
            remaining_diff=to_diff_response(run_id, remaining),
        )
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScoreConcurrentModificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/cancel", response_model=CancelResponse)
def cancel_agent_run(
    run_id: str,
    service: AgentRunService = Depends(get_agent_run_service),
) -> CancelResponse:
    """#46: エージェント run をキャンセルし、ステージング領域を破棄する(FR-23)。

    重要: current.json は1バイトも変更されない。
    """
    try:
        result = service.cancel(run_id)
        return CancelResponse(
            run_id=result["run_id"],
            status=result["status"],
            project_id=result["project_id"],
        )
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/report", response_model=AgentReportResponse)
def get_agent_report(
    run_id: str,
    service: AgentRunService = Depends(get_agent_run_service),
) -> AgentReportResponse:
    """#47: run_id のワークスペースから成果報告 report.md を取得する(設計書§8.6, §11.3)。"""
    try:
        content = service.get_report(run_id)
        return AgentReportResponse(run_id=run_id, content=content)
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
