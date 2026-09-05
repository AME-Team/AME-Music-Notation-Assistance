"""プロジェクト CRUD API(#11, FR-01)。

メディア配信(`/audio/*` `/analysis/*`)は `api/media.py` にある(§14)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app.api.deps import get_project_service
from app.api.schemas import Project, ProjectList
from app.services.project_service import (
    ProjectNotFoundError,
    ProjectService,
    UnsupportedAudioFormatError,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=Project, status_code=201)
async def create_project(
    file: UploadFile, service: ProjectService = Depends(get_project_service)
) -> dict:
    content = await file.read()
    try:
        return service.create_project(original_filename=file.filename or "upload", content=content)
    except UnsupportedAudioFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=ProjectList)
async def list_projects(service: ProjectService = Depends(get_project_service)) -> dict:
    return {"projects": service.list_projects()}


@router.get("/{project_id}", response_model=Project)
async def get_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> dict:
    try:
        return service.get_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> None:
    try:
        service.delete_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
