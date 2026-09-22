"""プロジェクト CRUD API(#11, FR-01)。

メディア配信(`/audio/*` `/analysis/*`)は `api/media.py` にある(§14)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_project_service, get_settings
from app.api.schemas import Project, ProjectList
from app.config import Settings
from app.services.project_archive_service import (
    InvalidArchiveError,
    export_project_archive,
    import_project_archive,
)
from app.services.project_service import (
    ProjectNotFoundError,
    ProjectService,
    UnsupportedAudioFormatError,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

_ARCHIVE_MEDIA_TYPE = "application/zip"


@router.post("", response_model=Project, status_code=201)
async def create_project(
    file: UploadFile, service: ProjectService = Depends(get_project_service)
) -> dict:
    content = await file.read()
    try:
        return service.create_project(original_filename=file.filename or "upload", content=content)
    except UnsupportedAudioFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/import", response_model=Project, status_code=201)
def import_project(
    file: UploadFile,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """#65 FR-18: 単一アーカイブ(`.ameproj`)からプロジェクトを新規作成する。

    `export_project_archive`と対になる読み込み側。`create_project`と異なり
    `async def`にしない(`export_score`/`get_peaks`と同じ理由、#27-M2レビュー
    指摘): zip展開・DB書き込みはCPU/IOバウンドな同期処理で、`include_stems`
    付きのアーカイブは数十MB規模になりうるため、`async def`のままだと
    イベントループを直接ブロックし、SSEでのジョブ進捗配信など他の同時
    リクエストを止めてしまう。`UploadFile.file`(同期`SpooledTemporaryFile`)
    経由で読む。
    """
    content = file.file.read()
    try:
        return import_project_archive(settings.workspace_dir, service, content)
    except InvalidArchiveError as exc:
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


@router.get(
    "/{project_id}/archive",
    response_class=FileResponse,
    responses={
        200: {"content": {_ARCHIVE_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}}}
    },
)
def get_project_archive(
    project_id: str,
    include_stems: bool = Query(default=True),
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """#65 FR-18: プロジェクトを単一アーカイブ(`.ameproj`)として書き出す。

    `export_score`と同じ理由で`async def`にしない(zip書き込みはCPU/IOバウンドな
    同期処理)。`include_stems=false`でステムWAV(サイズの大半を占める)を除外できる。
    """
    try:
        archive_path = export_project_archive(
            settings.workspace_dir, project_id, service, include_stems=include_stems
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    return FileResponse(archive_path, media_type=_ARCHIVE_MEDIA_TYPE, filename=archive_path.name)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str, service: ProjectService = Depends(get_project_service)
) -> None:
    try:
        service.delete_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
