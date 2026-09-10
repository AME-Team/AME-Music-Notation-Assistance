"""FastAPI 依存関係: リクエストから app.state のサービスを取り出す。"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import Settings
from app.domain.score import ScoreIR
from app.services.job_service import JobManager
from app.services.project_service import ProjectNotFoundError, ProjectService
from app.services.score_service import ScoreNotFoundError, ScoreService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_project_service(request: Request) -> ProjectService:
    return request.app.state.project_service


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def ensure_project_exists(project_id: str, service: ProjectService) -> dict:
    """プロジェクトの存在を確認し、取得済みレコードを返す(#21/#23/#27で共有)。

    戻り値は大半の呼び出し元では無視されるが、`api/media.py`の`get_peaks`は
    原曲パス解決に必要な`audio_format`をここから再利用することで、
    `service.audio_path()`が内部で行う`get_project`の再呼び出し(DBラウンド
    トリップの重複)を避けている。
    """
    try:
        return service.get_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc


def read_score_or_404(project_id: str, settings: Settings) -> ScoreIR:
    """Score IRを読み込み、未作成(Stage 3=採譜未実行)なら404を返す(#23/#27で共有)。

    `api/score.py`/`api/export.py`の両方が同じ変換(`ScoreNotFoundError`→404)を
    必要とするため、404の文言(「run the transcribe stage first」)が将来両者で
    乖離しないよう1箇所に集約する(#27-M2レビュー指摘)。
    """
    try:
        return ScoreService(workspace_dir=settings.workspace_dir).read_score(project_id)
    except ScoreNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="score not found; run the transcribe stage first"
        ) from exc
