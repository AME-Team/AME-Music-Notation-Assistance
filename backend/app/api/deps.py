"""FastAPI 依存関係: リクエストから app.state のサービスを取り出す。"""

from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.services.job_service import JobManager
from app.services.project_service import ProjectService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_project_service(request: Request) -> ProjectService:
    return request.app.state.project_service


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager
