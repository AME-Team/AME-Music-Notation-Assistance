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
import collections
import contextlib
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

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

# #49 Gate2レビュー指摘(LOW): `_event_history`/`_provider_run_ids`/
# `_provider_instances`はrun完了後も削除されず、サーバ稼働中にrun数分だけ
# 増え続ける。終了済みrunがこの件数を超えたら、最も古いものから解放する
# (実行中のrunは対象にしない — `_completed_run_order`には終端到達時にのみ追加する)。
_MAX_TRACKED_TERMINAL_RUNS: Final = 200


class UnknownProviderError(ValueError):
    """未知の`provider`名が要求された場合。"""


class UnknownTaskTypeError(ValueError):
    """未知の`task_type`が要求された場合。"""


class MissingPromptError(ValueError):
    """`task_type="investigate"`(自然言語の自由指示、§8.7)に`prompt`が無い場合。"""


def _compose_prompt(
    task_def: TaskDefinition, user_prompt: str | None, scope: dict[str, Any] | None
) -> str:
    """タスク定義の目的/手順 + スコープ + ユーザー指示を、実際にプロバイダへ渡す

    1本のpromptへ組み立てる。

    `task_def.prompt_template`が設定されているタスク(#50時点では
    consistency-pass/voicing-fixのみ)はその具体的な作業手順を埋め込む。
    それ以外のタスクは目的レベルの記述のみの汎用プロンプトにフォールバックする
    (各タスクの実プロンプトの作り込みはM6でこのフィールドを埋めていく想定)。

    `scope`はTASK.mdにも書き込まれる(`create_workspace`側)が、
    エージェントが最初に読むメインプロンプト自体にも埋め込む — 特に
    voicing-fixは「指定範囲」が作業の前提そのものであり、TASK.mdを読むまで
    スコープを知らない状態を作らない。
    """
    lines = [f"タスク種別: {task_def.id}", f"目的: {task_def.purpose}", ""]
    if scope:
        lines.append(f"対象スコープ: {scope}")
        lines.append("")
    if task_def.prompt_template:
        lines.append(task_def.prompt_template)
        lines.append("")
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
    _completed_run_order: collections.deque[str] = field(
        default_factory=collections.deque, init=False
    )

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

        composed_prompt = _compose_prompt(task_def, prompt, scope)
        # 呼び出し元が明示的に`budget`を渡した場合はそれを優先し、無指定なら
        # タスク定義の既定予算(#50: consistency-pass/voicing-fixはturns_maxと
        # 同様にタスクごとに調整済み)を使う。
        max_tokens_budget = budget if budget is not None else task_def.max_tokens_budget
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
                max_tokens_budget=max_tokens_budget,
                timeout_sec=task_def.timeout_sec,
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
        timeout_sec: int | None,
    ) -> None:
        # JobManager._run_jobと同じレース対策(#13): create_run()がこのコルーチンを
        # asyncio.create_taskでスケジュールした直後にcancel_run()が呼ばれると、
        # このコルーチンが実行され始める前にrunは既にキャンセル要求済みのことがある。
        if run_id in self._cancel_requested:
            self._cancel_requested.discard(run_id)
            self.agent_run_service.update_status(run_id, "cancelled")
            await self._close_subscribers(run_id)
            self._forget_oldest_terminal_runs_if_over_capacity(run_id)
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
            self._forget_oldest_terminal_runs_if_over_capacity(run_id)
            return

        task_kwargs: dict[str, Any] = {}
        if max_tokens_budget is not None:
            task_kwargs["max_tokens_budget"] = max_tokens_budget
        # #50: timeout_secもタスク定義の既定値(未指定ならAgentTask自身の
        # 既定900秒)で上書きする。#49時点ではmax_turns/max_tokens_budgetのみが
        # AgentTaskへ届いており、timeout_secはどのタスクでも常に既定値固定だった。
        if timeout_sec is not None:
            task_kwargs["timeout_sec"] = timeout_sec
        task = AgentTask(
            task_type=task_type,
            project_id=project_id,
            prompt=prompt,
            workspace=workspace,
            mcp_servers=[
                McpServerSpec(
                    name="score",
                    kind="in_process",
                    # `run_id`(公開run_id)を明示的に渡す(#50で発見した実バグの
                    # 修正): ClaudeAgentProviderがこれを自身のprovider内部run_id
                    # として採用しないと、score_apply_opsが書き込むstaging先
                    # (score/staging/{run_id}.json)がこのManagerの公開run_idと
                    # 食い違い、AgentRunService.accept/reject/get_diff(#46)が
                    # 永遠にステージング済みの変更を見つけられなくなる。
                    config={"workspace_dir": str(self.workspace_dir), "run_id": run_id},
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
            self._forget_oldest_terminal_runs_if_over_capacity(run_id)
            return

        self._provider_run_ids[run_id] = handle.run_id
        self._provider_instances[run_id] = provider

        if run_id in self._cancel_requested:
            with contextlib.suppress(Exception):
                await provider.cancel(handle.run_id)

        # #49 Gate2レビュー指摘(MIDDLE): workspace構築/provider.start()は
        # 例外を捕捉してfailed化していたが、ここ(stream()の駆動とresult()の
        # 取得)は無防備だった。想定外の例外でこのバックグラウンドタスクが
        # 例外終了すると、runがstatus="running"のまま固定され、
        # _close_subscribers()も呼ばれずSSE購読者がNoneを受け取れずハングする。
        try:
            async for event in provider.stream(handle.run_id):
                # provider内部run_id(handle.run_id)を公開run_idへ書き換えてから
                # 配信する(モジュールdocstring参照): SSE購読者はURLの{run_id}と
                # 一致するdata.run_idを期待する(§11.4)。
                public_event = dataclasses.replace(event, run_id=run_id)
                await self._publish(run_id, public_event)

            result = await provider.result(handle.run_id)
            # #49 Gate2レビュー指摘(MIDDLE): provider.cancel()はcontextlib.suppressで
            # 包まれたベストエフォートであり、実際には中断できないprovider(例:
            # cancelを無視してstreamを最後まで流す実装)では、ここでstream完了後の
            # result.status="completed"がcancel_run()側で既に確定させた"cancelled"を
            # 上書きしてしまう。_cancel_requestedはfinallyまで残るため、この時点で
            # 一度でもcancel_run()が呼ばれていればAgentRunService.cancel()が確定させた
            # 状態を優先し、ここでは上書きしない。
            if run_id not in self._cancel_requested:
                self.agent_run_service.update_status(
                    run_id,
                    result.status,
                    turns=result.turns,
                    usage=dataclasses.asdict(result.usage),
                    staged_ops_count=result.staged_ops_count,
                    error=result.error,
                )
        except Exception as exc:  # noqa: BLE001 - stream()/result()の想定外例外もfailed化する
            if run_id not in self._cancel_requested:
                self.agent_run_service.update_status(run_id, "failed", error=str(exc))
        finally:
            self._cancel_requested.discard(run_id)
            await self._close_subscribers(run_id)
            self._forget_oldest_terminal_runs_if_over_capacity(run_id)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        """runをキャンセルする(FR-23)。ライブなprovider runがあればベストエフォートで

        `interrupt`を試み、その後`AgentRunService.cancel()`(ステージング破棄・
        status更新・監査ログ記録、#46)へ委譲する。`AgentRunNotFoundError`/
        `ValueError`(既にcompleted)は`agent_run_service.cancel()`からそのまま
        伝播させる — ただしその場合も`_cancel_requested`に追加した`run_id`は
        `finally`で必ず取り除く(#49 Gate2レビュー指摘・LOW: 例外時に残留すると、
        別の(将来の)同名run_idや後続のリトライ処理に誤って影響しうる)。
        """
        self._cancel_requested.add(run_id)
        try:
            provider = self._provider_instances.get(run_id)
            provider_run_id = self._provider_run_ids.get(run_id)
            if provider is not None and provider_run_id is not None:
                with contextlib.suppress(Exception):
                    await provider.cancel(provider_run_id)
            return self.agent_run_service.cancel(run_id)
        except Exception:
            self._cancel_requested.discard(run_id)
            raise

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """SSE購読用のQueueを新規登録する。

        実行中・終了済みを問わず、まず`_publish`が蓄積済みの`AgentEvent`履歴を
        即座に積む(#49 Gate2レビュー指摘・MIDDLE: 以前は終了済みrunのみ履歴を
        再生しており、`POST .../runs`の202応答からSSE購読開始までの間に発行済みの
        イベント(最初のthinking等)が実行中runの購読者には届かなかった)。
        既に終了しているrunならそのまま`None`(終端)で閉じ、実行中なら続けて
        `_subscribers`へ登録しライブ配信を受け取れるようにする(`JobManager.
        subscribe`と同じ「後から購読しても最終状態を受け取れる」パターン —
        ジョブ側は最終状態1件のみだが、agent runは全ツール呼び出し履歴を再生できる)。

        既存購読者が複数いる状態で新規購読が割り込むごく狭いタイミング
        (`_publish`が複数queueへ`await queue.put`している最中)では、新規購読者が
        直近1件を履歴再生とライブ配信の両方で二重に受け取る可能性が理論上残る
        (このメソッド自体は同期だが`_publish`側は各queueへのawaitの間で
        中断されうるため)。実害は同一イベントの重複程度であり、seqでの
        重複排除は呼び出し元(SSEクライアント)側の責務とする。
        """
        queue: asyncio.Queue = asyncio.Queue()
        run = self.agent_run_service.get_run(run_id)  # raises AgentRunNotFoundError
        for event in self._event_history.get(run_id, []):
            queue.put_nowait(event)
        if run["status"] in _TERMINAL_STATUSES:
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

    def _forget_oldest_terminal_runs_if_over_capacity(self, run_id: str) -> None:
        """#49 Gate2レビュー指摘(LOW): 終了済みrunの`_event_history`等を無制限に

        保持せず、`_MAX_TRACKED_TERMINAL_RUNS`件を超えたら最も古いものから解放する。
        `_drive_run`が終端状態へ遷移した直後(全ての早期return経路を含む)に呼ぶ。
        実行中のrunは`_completed_run_order`に載らないため解放対象にならない。
        """
        self._completed_run_order.append(run_id)
        while len(self._completed_run_order) > _MAX_TRACKED_TERMINAL_RUNS:
            oldest = self._completed_run_order.popleft()
            self._event_history.pop(oldest, None)
            self._provider_run_ids.pop(oldest, None)
            self._provider_instances.pop(oldest, None)
