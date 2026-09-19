"""L2 Coding Agent API(#42, #46, #47, #49, 設計書§11.3)。

- `GET /api/agent/providers`: プロバイダ一覧(#42)
- `POST /api/projects/{project_id}/agent/runs`: run起動(#49)
- `GET /api/agent/runs/{run_id}`: run状態 `{status, turns, usage, staged_ops_count}`(#49)
- `GET /api/agent/runs/{run_id}/events`: **SSE**でAgentEventを逐次配信(#49, FR-20)
- `GET /api/agent/runs/{run_id}/audit`: 監査ログ(#49, FR-22)
- `GET /api/agent/tasks`: 標準タスク定義一覧(#49, §8.7)
- `GET /api/agent/runs/{run_id}/diff`: ステージング vs current の差分(#46)
- `POST /api/agent/runs/{run_id}/accept`: 差分の承認(スコープ指定対応)(#46)
- `POST /api/agent/runs/{run_id}/reject`: 差分の却下(スコープ指定対応)(#46)
- `POST /api/agent/runs/{run_id}/cancel`: キャンセル(ステージング破棄・current無変更)(#46, #49)
- `GET /api/agent/runs/{run_id}/report`: 成果報告 report.md の取得(#47)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.audit import AuditEntry
from app.agent.provider import AgentEvent, AgentRunNotFoundError
from app.agent.providers.claude import ClaudeAgentProvider
from app.agent.providers.dummy import DummyAgentProvider
from app.agent.tasks import STANDARD_TASKS
from app.agent.workspace import ReportNotFoundError
from app.api.deps import get_agent_run_manager, get_agent_run_service
from app.api.diff import (
    AcceptResponse,
    DiffResponse,
    RejectResponse,
    ScopeRequest,
    to_diff_response,
)
from app.pipeline.refine.l1_client import is_claude_cli_available
from app.services.agent_run_manager import (
    AgentRunManager,
    MissingPromptError,
    MissingScopeError,
    UnknownProviderError,
    UnknownTaskTypeError,
)
from app.services.agent_run_service import (
    AgentRunService,
    ScoreConcurrentModificationError,
)
from app.services.project_service import ProjectNotFoundError
from app.services.score_service import ScoreNotFoundError

router = APIRouter(prefix="/api/agent", tags=["agent"])

# `POST /api/projects/{project_id}/agent/runs`だけは`/api/agent`プレフィックスに
# 収まらない(パスの主語がproject)ため、`api/jobs.py`の
# `POST /api/projects/{project_id}/stages/{stage}/run`と同じ理由でプレフィックス無しの
# 別routerに分ける。
project_router = APIRouter(tags=["agent"])


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


class CreateAgentRunRequest(BaseModel):
    """§11.3: `POST /api/projects/{id}/agent/runs`のリクエストボディ。

    `task_type="investigate"`(自然言語の自由指示、§8.7)の場合は`prompt`が必須
    (`AgentRunManager.create_run`が検証する)。それ以外のタスクでは、
    `prompt`はタスク定義の目的文へ追加される補足指示として扱われる(#50が
    各タスクの実プロンプトを作り込むまでの暫定挙動)。
    """

    task_type: str
    scope: dict[str, Any] | None = None
    prompt: str | None = None
    provider: str = "claude"
    model: str | None = None
    budget: int | None = None


class CreateAgentRunResponse(BaseModel):
    run_id: str


class AgentRunStatusResponse(BaseModel):
    run_id: str
    project_id: str
    status: str
    turns: int
    usage: dict[str, int] | None = None
    staged_ops_count: int
    error: str | None = None


class TaskDefinitionResponse(BaseModel):
    id: str
    purpose: str
    tools: tuple[str, ...]
    turns_min: int | None
    turns_max: int
    # #51 AgentTaskLauncher: `requires_scope=True`のタスク(voicing-fix等)を
    # フロントエンドが`task_type`文字列のハードコードなしに判別できるよう、
    # `TaskDefinition.requires_scope`(#50)をそのまま公開する。
    requires_scope: bool = False


def _event_to_sse_payload(event: AgentEvent) -> dict[str, Any]:
    """`AgentEvent`を§11.4のSSEペイロード形状(`ts`は含まない)へ変換する。"""
    payload: dict[str, Any] = {
        "run_id": event.run_id,
        "seq": event.seq,
        "kind": event.kind,
        "tool_name": event.tool_name,
        "payload": event.payload,
    }
    if event.usage is not None:
        payload["usage"] = dataclasses.asdict(event.usage)
    return payload


@router.get("/providers", response_model=list[AgentProviderInfo])
def list_providers() -> list[AgentProviderInfo]:
    """#48/#52でプロバイダが増えるたびにここへ追記する想定の静的レジストリ。

    `name`は各プロバイダクラスの`name`属性を参照する(#42 Gate2レビュー指摘・
    2巡目 LOW: 文字列リテラルをここへ直書きすると`DummyAgentProvider.name`との
    二重管理になり、片方だけ更新漏れが起こりうる)。`dummy`は外部依存が無く
    常に実行可能なため`configured=True`固定。

    `claude`の`configured`は`is_claude_cli_available()`(#104、`l1_client.py`)を
    再利用する: `claude-agent-sdk`は同梱のClaude Code CLIをサブプロセスとして
    起動するため、L1と同じ「CLIがPATH上にあり認証済みか」がL2でも同じ実行可否の
    条件になる(APIキーの有無ではなくCLI認証状態で判定する、#104と同じ思想)。
    """
    return [
        AgentProviderInfo(name=DummyAgentProvider.name, configured=True),
        AgentProviderInfo(name=ClaudeAgentProvider.name, configured=is_claude_cli_available()),
    ]


@router.get("/tasks", response_model=list[TaskDefinitionResponse])
def list_task_definitions() -> list[TaskDefinitionResponse]:
    """#49: 標準タスク定義一覧(設計書§8.7)。"""
    return [
        TaskDefinitionResponse(
            id=t.id,
            purpose=t.purpose,
            tools=t.tools,
            turns_min=t.turns_min,
            turns_max=t.turns_max,
            requires_scope=t.requires_scope,
        )
        for t in STANDARD_TASKS
    ]


@project_router.post(
    "/api/projects/{project_id}/agent/runs",
    response_model=CreateAgentRunResponse,
    status_code=202,
)
async def create_agent_run(
    project_id: str,
    body: CreateAgentRunRequest,
    manager: AgentRunManager = Depends(get_agent_run_manager),
) -> CreateAgentRunResponse:
    """#49: L2エージェントrunを起動する(§11.3)。202を返した時点ではまだ`running`

    (`AgentRunManager.create_run`はワークスペース構築・`AgentProvider.start()`を
    バックグラウンドタスクで行う、`POST /api/projects/{id}/stages/{stage}/run`と
    同じ非同期起動パターン)。
    """
    try:
        run_id = await manager.create_run(
            project_id=project_id,
            task_type=body.task_type,
            scope=body.scope,
            prompt=body.prompt,
            provider_name=body.provider,
            model=body.model,
            budget=body.budget,
        )
    except UnknownTaskTypeError as exc:
        raise HTTPException(status_code=422, detail=f"unknown task_type: {exc}") from exc
    except UnknownProviderError as exc:
        raise HTTPException(status_code=422, detail=f"unknown provider: {exc}") from exc
    except MissingPromptError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MissingScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CreateAgentRunResponse(run_id=run_id)


@router.get("/runs/{run_id}", response_model=AgentRunStatusResponse)
def get_agent_run_status(
    run_id: str,
    service: AgentRunService = Depends(get_agent_run_service),
) -> AgentRunStatusResponse:
    """#49: run状態 `{status, turns, usage, staged_ops_count}`(§11.3)。"""
    try:
        run = service.get_run(run_id)
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AgentRunStatusResponse(
        run_id=run["id"],
        project_id=run["project_id"],
        status=run["status"],
        turns=run["turns"],
        usage=run["usage"],
        staged_ops_count=run["staged_ops_count"],
        error=run["error"],
    )


@router.get("/runs/{run_id}/events")
async def agent_run_events(
    run_id: str,
    manager: AgentRunManager = Depends(get_agent_run_manager),
) -> StreamingResponse:
    """#49: `event: agent`形式のSSEでAgentEventを逐次配信する(§11.4、FR-20)。

    `job_events`(`api/jobs.py`)と全く同じ枠組み(`manager.subscribe`が返す
    `asyncio.Queue`を`None`終端まで読み続ける、切断は`CancelledError`で検知して
    購読解除する)。**`async def`にする**(`job_events`と同様): `manager.subscribe`/
    `_publish`は`asyncio.Queue`と`self._subscribers`/`self._event_history`を
    イベントループのスレッドからのみ触れる前提で書かれている — 素の`def`だと
    FastAPIがスレッドプールで実行するため、`_drive_run`(イベントループ側)と
    別スレッドから同じ`asyncio.Queue`/dictへ同時にアクセスすることになり、
    `asyncio.Queue`はスレッドセーフではないためデータ破損やハングを起こしうる。
    """
    try:
        queue = manager.subscribe(run_id)
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def event_stream() -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                data = json.dumps(_event_to_sse_payload(event), ensure_ascii=False)
                yield f"event: agent\ndata: {data}\n\n"
        except asyncio.CancelledError:
            manager.unsubscribe(run_id, queue)
            raise

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/runs/{run_id}/audit", response_model=list[AuditEntry])
def get_agent_audit_log(
    run_id: str,
    service: AgentRunService = Depends(get_agent_run_service),
) -> list[AuditEntry]:
    """#49: 監査ログ(`audit.jsonl`)を返す(FR-22)。"""
    try:
        return service.get_audit_log(run_id)
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
async def cancel_agent_run(
    run_id: str,
    manager: AgentRunManager = Depends(get_agent_run_manager),
) -> CancelResponse:
    """#46/#49: エージェント run をキャンセルする(FR-23)。

    ライブなprovider runがあればベストエフォートで中断を要求し(#49)、
    その後ステージング領域を破棄する(#46)。重要: current.json は1バイトも
    変更されない。
    """
    try:
        result = await manager.cancel_run(run_id)
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
