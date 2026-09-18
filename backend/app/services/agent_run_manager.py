"""Agent Run Manager(#49, §5.1/§11.3/§11.4)— AgentProvider を起動し AgentEvent を

SSE へ中継する。

`JobManager`(#13)と同じ設計(SQLiteの状態テーブル + `asyncio.Queue`によるSSE
購読者へのファンアウト)を踏襲するが、DSP Worker(サブプロセス・CPUバウンド・
直列)とは完全に独立している(§5.1「DSP WorkerとAgent Runtimeを分離する」)。
エージェントrunはインプロセスの`AgentProvider.stream()`を直接`async for`で
駆動するだけであり、サブプロセスもキューの共有も無い — `job_service.py`の
`_processes`/`VALID_STAGES`のような概念はここには存在しない。

`AgentProvider.start()`は呼び出しごとに独自の`run_id`を採番する(公開APIの
`run_id`とは別物、`provider.py`参照)。このManagerは`public run_id → provider
インスタンス・provider内部run_id`のマッピングを保持し、`stream()`が返す
`AgentEvent.run_id`をSSE配信直前に公開run_idへ書き換える。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.provider import (
    AgentEvent,
    AgentProvider,
    AgentTask,
    McpServerSpec,
)
from app.agent.providers.claude import ClaudeAgentProvider
from app.agent.providers.dummy import DummyAgentProvider
from app.agent.tasks import TaskDefinition, get_task_definition
from app.infra import ids
from app.services.agent_run_service import AgentRunService

_TERMINAL_STATUSES = frozenset({"completed", "failed", "truncated", "cancelled"})


class UnknownProviderError(ValueError):
    """未知の`provider`名が要求された場合。"""


class UnknownTaskTypeError(ValueError):
    """未知の`task_type`が要求された場合。"""


class MissingPromptError(ValueError):
    """`task_type="investigate"`(自然言語の自由指示、§8.7)に`prompt`が無い場合。"""


def _compose_prompt(task_def: TaskDefinition, user_prompt: str | None) -> str:
    """タスク定義の目的 + ユーザー指示を、実際にプロバイダへ渡す1本のpromptへ組み立てる。

    タスクごとの詳細なプロンプトテンプレートの作り込みは#50の担当(§8.7の表は
    「目的」レベルの記述のみで、実文面までは規定していない)。#49ではエージェントが
    実際にツール呼び出しを開始できる最小限の文面を組み立てる。
    """
    lines = [f"タスク種別: {task_def.id}", f"目的: {task_def.purpose}", ""]
    if user_prompt:
        lines.append("追加指示:")
        lines.append(user_prompt)
        lines.append("")
    lines.append(
        "作業を始める前に、ワークスペース直下の TASK.md / context.md / "
        "notation_rules.md を読んでください。完了時には必ず report.md を作成してください。"
    )
    return "\n".join(lines)


@dataclass
class AgentRunManager:
    workspace_dir: Path
    agent_run_service: AgentRunService
    providers: dict[str, AgentProvider] = field(default_factory=dict)
    _provider_run_ids: dict[str, str] = field(default_factory=dict, init=False)
    _provider_instances: dict[str, AgentProvider] = field(default_factory=dict, init=False)
    _event_history: dict[str, list[AgentEvent]] = field(default_factory=dict, init=False)
    _subscribers: dict[str, list[asyncio.Queue]] = field(default_factory=dict, init=False)
    _cancel_requested: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        # テストからは`providers={"dummy": DummyAgentProvider()}`のように
        # 差し替えて、ClaudeAgentProviderに一切触れずに検証できるようにする。
        if not self.providers:
            self.providers = {
                DummyAgentProvider.name: DummyAgentProvider(),
                ClaudeAgentProvider.name: ClaudeAgentProvider(),
            }

    async def create_run(
        self,
        *,
        project_id: str,
        task_type: str,
        scope: dict[str, Any] | None = None,
        prompt: str | None = None,
        provider_name: str = "claude",
        model: str | None = None,
        budget: int | None = None,
    ) -> str:
        """run を登録し、実行を`asyncio.create_task`でバックグラウンド起動する。

        `ProjectNotFoundError`(存在しないproject_id)は
        `self.agent_run_service.create_run()`から自然に伝播させる。
        """
        task_def = get_task_definition(task_type)
        if task_def is None:
            raise UnknownTaskTypeError(task_type)
        if task_def.id == "investigate" and not prompt:
            raise MissingPromptError("task_type='investigate' requires a non-empty prompt")
        provider = self.providers.get(provider_name)
        if provider is None:
            raise UnknownProviderError(provider_name)

        run_id = ids.new_id("run")
        self.agent_run_service.create_run(run_id=run_id, project_id=project_id)

        composed_prompt = _compose_prompt(task_def, prompt)
        asyncio.create_task(  # noqa: RUF006 - fire-and-forget(JobManager._run_jobと同じ設計)
            self._drive_run(
                run_id,
                provider=provider,
                project_id=project_id,
                task_type=task_type,
                prompt=composed_prompt,
                scope=scope,
                allowed_tools=list(task_def.allowed_tools),
                model=model,
                max_turns=task_def.turns_max,
                max_tokens_budget=budget,
            )
        )
        return run_id

    async def _drive_run(
        self,
        run_id: str,
        *,
        provider: AgentProvider,
        project_id: str,
        task_type: str,
        prompt: str,
        scope: dict[str, Any] | None,
        allowed_tools: list[str],
        model: str | None,
        max_turns: int,
        max_tokens_budget: int | None,
    ) -> None:
        # JobManager._run_jobと同じレース対策(#13): create_run()がこのコルーチンを
        # asyncio.create_taskでスケジュールした直後にcancel_run()が呼ばれると、
        # このコルーチンが実行され始める前にrunは既にキャンセル要求済みのことがある。
        if run_id in self._cancel_requested:
            self._cancel_requested.discard(run_id)
            self.agent_run_service.update_status(run_id, "cancelled")
            await self._close_subscribers(run_id)
            return

        try:
            workspace = self.agent_run_service.create_workspace(
                run_id,
                task_type=task_type,
                prompt=prompt,
                scope=scope,
                allowed_tools=allowed_tools,
                model=model,
                max_turns=max_turns,
            )
        except Exception as exc:  # noqa: BLE001 - ワークスペース構築失敗はrunをfailedにする
            self.agent_run_service.update_status(run_id, "failed", error=str(exc))
            await self._close_subscribers(run_id)
            return

        task_kwargs: dict[str, Any] = {}
        if max_tokens_budget is not None:
            task_kwargs["max_tokens_budget"] = max_tokens_budget
        task = AgentTask(
            task_type=task_type,
            project_id=project_id,
            prompt=prompt,
            workspace=workspace,
            mcp_servers=[
                McpServerSpec(
                    name="score",
                    kind="in_process",
                    config={"workspace_dir": str(self.workspace_dir)},
                )
            ],
            allowed_tools=allowed_tools,
            model=model,
            max_turns=max_turns,
            **task_kwargs,
        )

        try:
            handle = await provider.start(task)
        except Exception as exc:  # noqa: BLE001 - provider.start()自体の失敗もfailed化する
            self.agent_run_service.update_status(run_id, "failed", error=str(exc))
            await self._close_subscribers(run_id)
            return

        self._provider_run_ids[run_id] = handle.run_id
        self._provider_instances[run_id] = provider

        if run_id in self._cancel_requested:
            with contextlib.suppress(Exception):
                await provider.cancel(handle.run_id)

        async for event in provider.stream(handle.run_id):
            # provider内部run_id(handle.run_id)を公開run_idへ書き換えてから配信する
            # (モジュールdocstring参照): SSE購読者はURLの{run_id}と一致する
            # data.run_idを期待する(§11.4)。
            public_event = dataclasses.replace(event, run_id=run_id)
            await self._publish(run_id, public_event)

        result = await provider.result(handle.run_id)
        self.agent_run_service.update_status(
            run_id,
            result.status,
            turns=result.turns,
            usage=dataclasses.asdict(result.usage),
            staged_ops_count=result.staged_ops_count,
            error=result.error,
        )
        self._cancel_requested.discard(run_id)
        await self._close_subscribers(run_id)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        """runをキャンセルする(FR-23)。ライブなprovider runがあればベストエフォートで

        `interrupt`を試み、その後`AgentRunService.cancel()`(ステージング破棄・
        status更新・監査ログ記録、#46)へ委譲する。`AgentRunNotFoundError`/
        `ValueError`(既にcompleted)は`agent_run_service.cancel()`からそのまま
        伝播させる。
        """
        self._cancel_requested.add(run_id)
        provider = self._provider_instances.get(run_id)
        provider_run_id = self._provider_run_ids.get(run_id)
        if provider is not None and provider_run_id is not None:
            with contextlib.suppress(Exception):
                await provider.cancel(provider_run_id)
        return self.agent_run_service.cancel(run_id)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """SSE購読用のQueueを新規登録する。

        既に終了しているrunなら、`_publish`が蓄積した`AgentEvent`履歴全件を
        即座に積んでから`None`(終端)で閉じる(`JobManager.subscribe`と同じ
        「後から購読しても最終状態を受け取れる」パターン — ジョブ側は最終状態
        1件のみだが、agent runは全ツール呼び出し履歴を再生できる)。
        """
        queue: asyncio.Queue = asyncio.Queue()
        run = self.agent_run_service.get_run(run_id)  # raises AgentRunNotFoundError
        if run["status"] in _TERMINAL_STATUSES:
            for event in self._event_history.get(run_id, []):
                queue.put_nowait(event)
            queue.put_nowait(None)
        else:
            self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs and queue in subs:
            subs.remove(queue)

    async def _publish(self, run_id: str, event: AgentEvent) -> None:
        self._event_history.setdefault(run_id, []).append(event)
        for queue in self._subscribers.get(run_id, []):
            await queue.put(event)

    async def _close_subscribers(self, run_id: str) -> None:
        for queue in self._subscribers.get(run_id, []):
            await queue.put(None)
        self._subscribers.pop(run_id, None)
