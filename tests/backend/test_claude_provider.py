"""#48: ClaudeAgentProvider(`app/agent/providers/claude.py`)のテスト。

`claude_agent_sdk.ClaudeSDKClient`は実プロセス(同梱Claude Code CLI)を起動する
ため、CI(APIキー無し)では動かせない。`app.agent.providers.claude.ClaudeSDKClient`
を`_FakeClient`へ差し替え、SDKの`connect`/`receive_response`/`interrupt`/
`disconnect`だけを模倣する(`_FakeClient`が生成するメッセージは全て
`claude_agent_sdk`の実dataclassをそのまま使う — メッセージ正規化ロジック
(`_normalize`/`_map_terminal_status`)は本物の型を見て分岐するため)。
"""

from __future__ import annotations

from collections.abc import Sequence
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

    def receive_response(self) -> _FakeMessageIterator:
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
    handle = await provider.start(task)

    with pytest.raises(claude_provider.ClaudeAgentProviderError):
        async for _ in provider.stream(handle.run_id):
            pass


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
    handle = await provider.start(task)

    with pytest.raises(claude_provider.ClaudeAgentProviderError):
        async for _ in provider.stream(handle.run_id):
            pass


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
