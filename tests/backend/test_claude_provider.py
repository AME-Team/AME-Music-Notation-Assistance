"""#48: ClaudeAgentProvider(`app/agent/providers/claude.py`)のテスト。

`claude_agent_sdk.ClaudeSDKClient`は実プロセス(同梱Claude Code CLI)を起動する
ため、CI(APIキー無し)では動かせない。`app.agent.providers.claude.ClaudeSDKClient`
を`_FakeClient`へ差し替え、SDKの`connect`/`receive_response`/`interrupt`/
`disconnect`だけを模倣する(`_FakeClient`が生成するメッセージは全て
`claude_agent_sdk`の実dataclassをそのまま使う — メッセージ正規化ロジック
(`_normalize`/`_map_terminal_status`)は本物の型を見て分岐するため)。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_contract import ProviderContractTests
from app.agent.provider import AgentTask, McpServerSpec
from app.agent.providers import claude as claude_provider
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

_HAPPY_SCRIPT: Sequence[Any] = [
    AssistantMessage(
        content=[TextBlock(text="analyzing")],
        model="claude-opus-5",
        usage={"input_tokens": 100, "output_tokens": 20},
    ),
    ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id="s1",
        terminal_reason="completed",
        total_cost_usd=0.01,
        usage={"input_tokens": 100, "output_tokens": 20},
    ),
]


class _FakeMessageIterator:
    def __init__(self, messages: Sequence[Any]) -> None:
        self._iter = iter(messages)

    def __aiter__(self) -> _FakeMessageIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeClient:
    """`ClaudeSDKClient`の最小限の代替。クラス変数`script`/`connect_error`/`calls`は

    テストごとに`_make_fake_client_cls`が新しいサブクラスを作ることで分離する。
    """

    script: Sequence[Any] = ()
    connect_error: Exception | None = None
    calls: list[_FakeClient]

    def __init__(self, options: Any) -> None:
        self.options = options
        self.interrupted = False
        self.disconnected = False
        type(self).calls.append(self)

    async def connect(self, prompt: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.prompt = prompt

    def receive_response(self) -> AsyncIterator[Any]:
        return _FakeMessageIterator(self.script)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


def _make_fake_client_cls(
    script: Sequence[Any] = (), *, connect_error: Exception | None = None
) -> type[_FakeClient]:
    return type(
        "_FakeClientSubclass",
        (_FakeClient,),
        {"script": script, "connect_error": connect_error, "calls": []},
    )


@pytest.fixture(autouse=True)
def _patch_claude_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_provider, "ClaudeSDKClient", _make_fake_client_cls(_HAPPY_SCRIPT)
    )


class TestClaudeProviderContract(ProviderContractTests):
    def provider(self) -> claude_provider.ClaudeAgentProvider:
        return claude_provider.ClaudeAgentProvider()


async def test_full_run_normalizes_text_and_result_into_events(tmp_path: Path) -> None:
    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do something", workspace=tmp_path
    )

    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert [e.kind for e in events] == ["text", "done"]
    assert events[0].payload["text"] == "analyzing"
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 100

    result = await provider.result(handle.run_id)
    assert result.status == "completed"
    assert result.turns == 1


async def test_tool_use_and_tool_result_are_correlated_and_staged_ops_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="t1", name="mcp__score__score_apply_ops", input={"ops": []}
                )
            ],
            model="claude-opus-5",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": '{"ok": true, "run_id": "r1"}'}],
                )
            ]
        ),
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="s1",
            terminal_reason="completed",
        ),
    ]
    monkeypatch.setattr(
        claude_provider, "ClaudeSDKClient", _make_fake_client_cls(script)
    )

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="edit the score",
        workspace=tmp_path,
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert [e.kind for e in events] == ["tool_use", "tool_result", "done"]
    assert events[0].tool_name == "mcp__score__score_apply_ops"
    assert events[1].tool_name == "mcp__score__score_apply_ops"
    assert events[1].payload["output"] == {"ok": True, "run_id": "r1"}

    result = await provider.result(handle.run_id)
    assert result.staged_ops_count == 1


async def test_result_message_is_error_maps_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        ResultMessage(
            subtype="error_during_execution",
            duration_ms=10,
            duration_api_ms=10,
            is_error=True,
            num_turns=1,
            session_id="s1",
            result="something went wrong",
        ),
    ]
    monkeypatch.setattr(
        claude_provider, "ClaudeSDKClient", _make_fake_client_cls(script)
    )

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do it", workspace=tmp_path
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "error"
    result = await provider.result(handle.run_id)
    assert result.status == "failed"
    assert result.error == "something went wrong"


async def test_max_turns_terminal_reason_maps_to_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        ResultMessage(
            subtype="error_max_turns",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=40,
            session_id="s1",
            terminal_reason="max_turns",
        ),
    ]
    monkeypatch.setattr(
        claude_provider, "ClaudeSDKClient", _make_fake_client_cls(script)
    )

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do it", workspace=tmp_path
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "done"
    result = await provider.result(handle.run_id)
    assert result.status == "truncated"


async def test_connect_error_before_any_message_retries_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cls = _make_fake_client_cls(connect_error=ClaudeSDKError("connection refused"))
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", fake_cls)
    # 指数バックオフの実待機を避ける(3回試行でも1e-3+2e-3秒程度で済ませる)。
    monkeypatch.setattr(claude_provider, "_BACKOFF_BASE_SEC", 0.001)

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do it", workspace=tmp_path
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "error"
    assert len(fake_cls.calls) == claude_provider._MAX_CONNECT_ATTEMPTS

    result = await provider.result(handle.run_id)
    assert result.status == "failed"


async def test_cli_not_found_error_fails_without_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cls = _make_fake_client_cls(connect_error=CLINotFoundError())
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", fake_cls)

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do it", workspace=tmp_path
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "error"
    assert len(fake_cls.calls) == 1

    result = await provider.result(handle.run_id)
    assert result.status == "failed"


async def test_connect_hang_respects_timeout_and_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate2レビュー指摘(MIDDLE): `client.connect()`自体は`asyncio.wait_for`の

    対象外で、CLIのハンドシェイクがハングすると`timeout_sec`を無視して
    無期限に`running`のままになっていたことの回帰テスト。
    """

    class _HangingConnectClient(_FakeClient):
        async def connect(self, prompt: str) -> None:
            await asyncio.sleep(10)

    _HangingConnectClient.calls = []
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", _HangingConnectClient)

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="do it",
        workspace=tmp_path,
        timeout_sec=1,
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "done"
    result = await provider.result(handle.run_id)
    assert result.status == "truncated"


async def test_backoff_respects_deadline_instead_of_sleeping_full_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate2レビュー指摘(MIDDLE): 接続リトライのバックオフ待機がdeadlineを

    考慮しておらず、timeout_secが小さい場合に合計待機がtimeout_secを
    超過しうったことの回帰テスト。バックオフ基準値をdeadlineより意図的に
    大きくし、deadline側でclampされなければこのテスト自体が10秒以上かかる
    ことで検出する。
    """
    fake_cls = _make_fake_client_cls(connect_error=ClaudeSDKError("connection refused"))
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", fake_cls)
    monkeypatch.setattr(claude_provider, "_BACKOFF_BASE_SEC", 10.0)

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="do it",
        workspace=tmp_path,
        timeout_sec=1,
    )
    handle = await provider.start(task)

    started = time.monotonic()
    events = [e async for e in provider.stream(handle.run_id)]
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, (
        "backoff sleep should be clamped to the remaining deadline, not 10s"
    )
    assert events[-1].kind == "done"
    result = await provider.result(handle.run_id)
    assert result.status == "truncated"


async def test_unexpected_non_sdk_exception_marks_run_failed_not_stuck_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate2レビュー指摘(MIDDLE): `_drive`が`ClaudeSDKError`以外の予期しない

    例外で中断した場合、`stream()`の`finally`が`run.status`を更新しないまま
    部分イベント列だけをキャッシュしており、runが`running`に固定され
    `result()`も完了状態を返せなくなっていたことの回帰テスト。
    """

    class _BrokenIterator:
        def __aiter__(self) -> _BrokenIterator:
            return self

        async def __anext__(self) -> Any:
            raise RuntimeError("boom: unexpected bug in message parsing")

    class _BrokenClient(_FakeClient):
        def receive_response(self) -> AsyncIterator[Any]:
            return _BrokenIterator()

    _BrokenClient.calls = []
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", _BrokenClient)

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do it", workspace=tmp_path
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "error"
    result = await provider.result(handle.run_id)
    assert result.status == "failed"

    # runがキャッシュ済みなので、再度stream()しても実SDKを叩かず同じ結果を再生する。
    events_again = [e async for e in provider.stream(handle.run_id)]
    assert events_again[-1].kind == "error"


async def test_unsupported_mcp_server_kind_raises_before_registering_run(
    tmp_path: Path,
) -> None:
    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="do it",
        workspace=tmp_path,
        mcp_servers=[McpServerSpec(name="score", kind="stdio", config={})],
    )

    with pytest.raises(claude_provider.ClaudeAgentProviderError):
        await provider.start(task)

    # runは登録されていないため、他の操作は全てAgentRunNotFoundErrorになる。
    assert provider._runs == {}


async def test_in_process_mcp_server_missing_workspace_dir_raises(
    tmp_path: Path,
) -> None:
    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="do it",
        workspace=tmp_path,
        mcp_servers=[McpServerSpec(name="score", kind="in_process", config={})],
    )

    with pytest.raises(claude_provider.ClaudeAgentProviderError):
        await provider.start(task)

    assert provider._runs == {}


async def test_budget_exceeded_result_message_maps_to_truncated_not_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate2レビュー指摘(MIDDLE): 予算超過による`interrupt()`後にSDKが返す

    `terminal_reason="aborted_streaming"`は、ユーザーによる`cancel()`と同じ値
    だが§8.9上は別の状態(truncated)でなければならない。
    """
    script = [
        AssistantMessage(
            content=[TextBlock(text="thinking")],
            model="claude-opus-5",
            usage={"input_tokens": 200_000, "output_tokens": 200_000},
        ),
        ResultMessage(
            subtype="aborted",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            terminal_reason="aborted_streaming",
        ),
    ]
    monkeypatch.setattr(
        claude_provider, "ClaudeSDKClient", _make_fake_client_cls(script)
    )

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="do it",
        workspace=tmp_path,
        max_tokens_budget=1000,
    )
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "done"
    result = await provider.result(handle.run_id)
    assert result.status == "truncated"


async def test_cancel_mid_stream_without_result_message_maps_to_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate2レビュー指摘(MIDDLE): `ResultMessage`を受け取らず`StopAsyncIteration`で

    終了した場合のフォールバックが、`cancel_requested`を無視して無条件に
    completedへ上書きしていた(状態機械の巻き戻り)ことの回帰テスト。
    """
    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do it", workspace=tmp_path
    )
    handle = await provider.start(task)

    class _CancelMidStreamIterator:
        def __init__(self) -> None:
            self._sent_first = False

        def __aiter__(self) -> _CancelMidStreamIterator:
            return self

        async def __anext__(self) -> Any:
            if not self._sent_first:
                self._sent_first = True
                return AssistantMessage(content=[TextBlock(text="working")], model="m")
            # 2件目の取得を試みたタイミングで、SDKクライアント側から
            # cancel()が飛んできた状況を模す(cancel()はrun.clientが
            # 設定済みならinterrupt()もベストエフォートで呼ぶ)。
            await provider.cancel(handle.run_id)
            raise StopAsyncIteration

    class _CancelMidStreamClient(_FakeClient):
        def receive_response(self) -> AsyncIterator[Any]:
            return _CancelMidStreamIterator()

    _CancelMidStreamClient.calls = []
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", _CancelMidStreamClient)

    events = [e async for e in provider.stream(handle.run_id)]

    assert events[-1].kind == "cancelled"
    result = await provider.result(handle.run_id)
    assert result.status == "cancelled"


async def test_in_process_mcp_server_is_wired_into_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cls = _make_fake_client_cls(_HAPPY_SCRIPT)
    monkeypatch.setattr(claude_provider, "ClaudeSDKClient", fake_cls)

    provider = claude_provider.ClaudeAgentProvider()
    task = AgentTask(
        task_type="test",
        project_id="proj-1",
        prompt="do it",
        workspace=tmp_path,
        mcp_servers=[
            McpServerSpec(
                name="score", kind="in_process", config={"workspace_dir": str(tmp_path)}
            )
        ],
        allowed_tools=["mcp__score__score_query"],
    )
    handle = await provider.start(task)
    [e async for e in provider.stream(handle.run_id)]

    assert len(fake_cls.calls) == 1
    options = fake_cls.calls[0].options
    assert "score" in options.mcp_servers
    assert options.allowed_tools == ["mcp__score__score_query"]
    assert options.cwd == str(tmp_path)
    assert options.permission_mode == "bypassPermissions"
