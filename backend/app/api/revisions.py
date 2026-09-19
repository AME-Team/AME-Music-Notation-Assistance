"""リビジョン管理 API エンドポイント(#59, FR-15, §10.3, §10.4)。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api import deps
from app.api.schemas import CreateRevisionRequest, RevisionDiffResponse, RevisionResponse
from app.config import Settings
from app.domain.score import ScoreIR
from app.services.project_service import ProjectService
from app.services.revision_service import (
    RevisionNotFoundError,
    RevisionService,
)
from app.services.score_service import ScoreNotFoundError

router = APIRouter(prefix="/api/projects/{project_id}/revisions", tags=["revisions"])


def _get_revision_service(settings: Settings = Depends(deps.get_settings)) -> RevisionService:
    return RevisionService(workspace_dir=settings.workspace_dir)


@router.post("", response_model=RevisionResponse, status_code=status.HTTP_201_CREATED)
def create_revision(
    project_id: str,
    body: CreateRevisionRequest,
    project_service: ProjectService = Depends(deps.get_project_service),
    revision_service: RevisionService = Depends(_get_revision_service),
) -> RevisionResponse:
    deps.ensure_project_exists(project_id, project_service)
    try:
        rev = revision_service.create_revision(
            project_id=project_id, name=body.name, description=body.description
        )
        return RevisionResponse(**rev.to_dict())
    except ScoreNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="score not found; run the transcribe stage first"
        ) from exc


@router.get("", response_model=list[RevisionResponse])
def list_revisions(
    project_id: str,
    project_service: ProjectService = Depends(deps.get_project_service),
    revision_service: RevisionService = Depends(_get_revision_service),
) -> list[RevisionResponse]:
    deps.ensure_project_exists(project_id, project_service)
    revs = revision_service.list_revisions(project_id)
    return [RevisionResponse(**r.to_dict()) for r in revs]


@router.get("/{revision_id}", response_model=RevisionResponse)
def get_revision(
    project_id: str,
    revision_id: str,
    project_service: ProjectService = Depends(deps.get_project_service),
    revision_service: RevisionService = Depends(_get_revision_service),
) -> RevisionResponse:
    deps.ensure_project_exists(project_id, project_service)
    try:
        rev = revision_service.get_revision(project_id, revision_id)
        return RevisionResponse(**rev.to_dict())
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"revision {revision_id!r} not found") from exc


@router.delete("/{revision_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_revision(
    project_id: str,
    revision_id: str,
    project_service: ProjectService = Depends(deps.get_project_service),
    revision_service: RevisionService = Depends(_get_revision_service),
) -> None:
    deps.ensure_project_exists(project_id, project_service)
    try:
        revision_service.delete_revision(project_id, revision_id)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"revision {revision_id!r} not found") from exc


@router.post("/{revision_id}/restore", response_model=ScoreIR)
def restore_revision(
    project_id: str,
    revision_id: str,
    project_service: ProjectService = Depends(deps.get_project_service),
    revision_service: RevisionService = Depends(_get_revision_service),
) -> ScoreIR:
    deps.ensure_project_exists(project_id, project_service)
    try:
        return revision_service.restore_revision(project_id, revision_id)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"revision {revision_id!r} not found") from exc


@router.get("/{revision_id}/diff", response_model=RevisionDiffResponse)
def get_revision_diff(
    project_id: str,
    revision_id: str,
    base_revision_id: str | None = Query(
        default=None, description="比較元リビジョンID(未指定時は現行スコア)"
    ),
    project_service: ProjectService = Depends(deps.get_project_service),
    revision_service: RevisionService = Depends(_get_revision_service),
) -> RevisionDiffResponse:
    deps.ensure_project_exists(project_id, project_service)
    try:
        diff = revision_service.compute_revision_diff(
            project_id, revision_id, base_revision_id=base_revision_id
        )
        return RevisionDiffResponse(**diff)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScoreNotFoundError as exc:
        raise HTTPException(status_code=404, detail="score not found") from exc
