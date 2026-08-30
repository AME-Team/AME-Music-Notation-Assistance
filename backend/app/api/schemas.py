"""共有 Pydantic モデル(#14: ここから OpenAPI → TS 型を生成する)。"""

from __future__ import annotations

from pydantic import BaseModel


class StageStatus(BaseModel):
    status: str
    progress: float


class Project(BaseModel):
    id: str
    name: str
    original_filename: str
    audio_format: str
    created_at: str
    stages: dict[str, StageStatus]


class ProjectList(BaseModel):
    projects: list[Project]


class RunStageRequest(BaseModel):
    params: dict = {}


class RunStageResponse(BaseModel):
    job_id: str


class Job(BaseModel):
    id: str
    project_id: str
    stage: str
    status: str
    progress: float
    message: str | None = None
    exit_code: int | None = None
    created_at: str
    updated_at: str


class ErrorResponse(BaseModel):
    detail: str
