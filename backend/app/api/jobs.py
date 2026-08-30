"""ジョブ機構と SSE 進捗配信(#13, §11.2, §11.4)。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import get_job_manager
from app.api.schemas import Job, RunStageRequest, RunStageResponse
from app.services.job_service import JobManager, JobNotFoundError, UnknownStageError

router = APIRouter(tags=["jobs"])


@router.post(
    "/api/projects/{project_id}/stages/{stage}/run",
    response_model=RunStageResponse,
    status_code=202,
)
async def run_stage(
    project_id: str,
    stage: str,
    body: RunStageRequest,
    manager: JobManager = Depends(get_job_manager),
) -> dict:
    try:
        job_id = await manager.create_job(project_id=project_id, stage=stage, params=body.params)
    except UnknownStageError as exc:
        raise HTTPException(status_code=422, detail=f"unknown stage: {exc}") from exc
    return {"job_id": job_id}


@router.get("/api/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, manager: JobManager = Depends(get_job_manager)) -> dict:
    try:
        return manager.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@router.post("/api/jobs/{job_id}/cancel", status_code=204)
async def cancel_job(job_id: str, manager: JobManager = Depends(get_job_manager)) -> None:
    try:
        await manager.cancel_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@router.get("/api/jobs/{job_id}/events")
async def job_events(
    job_id: str, manager: JobManager = Depends(get_job_manager)
) -> StreamingResponse:
    """§11.4: `event: progress` 形式の SSE。"""
    try:
        queue = manager.subscribe(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"event: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            manager.unsubscribe(job_id, queue)
            raise

    return StreamingResponse(event_stream(), media_type="text/event-stream")
