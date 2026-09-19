"""#53: プロバイダ契約・パリティテスト — 同一タスクが両プロバイダで動くこと。

設計書 §4.2 NFR-16 / §8.2 / §15 R-12 / §16 M5c 完了条件:
1. 同一の標準タスク (`consistency-pass`, `voicing-fix`) が両プロバイダで完走すること。
2. `AgentEvent` の系列が両プロバイダで同じ意味・構造を持つこと (イベント種別・順序・トークン集計)。
3. ポリシー違反 (ネットワーク遮断、ワークスペース保護) が両プロバイダで同様に拒否されること。
4. キャンセルが両プロバイダで正しく動作し、`current.json` が一切変更されないこと。
5. CI での実行戦略: モックによる決定論的テスト (CI 既定) と、実機/実APIキー環境用の skip 戦略 (@pytest.mark.slow)。
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.agent.policy import DISALLOWED_TOOLS, build_opencode_tools_config
from app.agent.provider import (
    AgentTask,
    McpServerSpec,
)
from app.agent.providers import claude as claude_module
from app.agent.providers.claude import ClaudeAgentProvider
from app.agent.providers.opencode import (
    OpenCodeProvider,
    generate_opencode_json,
    is_opencode_available,
)
from app.agent.tasks import TaskDefinition, get_task_definition
from app.domain.score import Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import storage
from app.services.agent_run_manager import _compose_prompt
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

_PROJECT_ID = "prj_parity"


# =========================================================================
# テスト用ヘルパ & モック構築
# =========================================================================


def _make_score() -> ScoreIR:
    return ScoreIR(
        project_id=_PROJECT_ID,
        source=SourceInfo(filename="test.wav", duration_sec=8.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[Part(id="piano", name="Piano", midi_program=0, staves=2)],
    )


def _seed_project(workspace_dir: Path) -> ScoreIR:
    score = _make_score()
    storage.write_json(
        storage.score_current_path(workspace_dir, _PROJECT_ID),
        score.model_dump(mode="json"),
    )
    storage.write_json(
        storage.beatmap_path(workspace_dir, _PROJECT_ID),
        {
            "beats": [
                {
                    "time_sec": i * 0.5,
                    "beat_in_bar": (i % 4) + 1,
                    "bar": (i // 4) + 1,
                }
                for i in range(16)
            ],
            "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
        },
    )
    return score


def _make_task(
    workspace_dir: Path,
    task_def: TaskDefinition,
    *,
    scope: dict[str, Any] | None = None,
) -> AgentTask:
    workspace = workspace_dir / "agent_workspace" / f"run_{task_def.id}"
    workspace.mkdir(parents=True, exist_ok=True)

    # TASK.md, context.md, notation_rules.md
    task_lines = [f"# Task {task_def.id}", ""]
    if scope:
        task_lines.extend(["## Scope", json.dumps(scope), ""])
    (workspace / "TASK.md").write_text("\n".join(task_lines) + "\n", encoding="utf-8")
    (workspace / "context.md").write_text("# Context\n", encoding="utf-8")
    (workspace / "notation_rules.md").write_text("# Rules\n", encoding="utf-8")

    prompt = _compose_prompt(task_def, None, scope)
    return AgentTask(
        task_type=task_def.id,
        project_id=_PROJECT_ID,
        prompt=prompt,
        workspace=workspace,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="in_process",
                config={"workspace_dir": str(workspace_dir)},
            )
        ],
        allowed_tools=list(task_def.allowed_tools),
        max_turns=task_def.turns_max,
        max_tokens_budget=task_def.max_tokens_budget or 300_000,
        timeout_sec=task_def.timeout_sec or 900,
    )


class _FakeClaudeIterator:
    def __init__(self, items: Sequence[Any]) -> None:
        self._iter = iter(items)

    def __aiter__(self) -> _FakeClaudeIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeClaudeClient:
    def __init__(self, script: Sequence[Any]) -> None:
        self.script = script
        self.interrupted = False
        self.disconnected = False
        self.options: Any = None

    async def connect(self, prompt: str) -> None:
        pass

    def receive_response(self) -> AsyncIterator[Any]:
        return _FakeClaudeIterator(self.script)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


class _MockOpenCodeTransport(httpx.AsyncBaseTransport):
    def __init__(self, sse_events: list[dict[str, Any]]) -> None:
        self.sse_events = sse_events
        self.abort_called = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        if url_path == "/session" and request.method == "POST":
            return httpx.Response(201, request=request, json={"id": "s_mock_parity"})

        if url_path.endswith("/abort") and request.method == "POST":
            self.abort_called = True
            return httpx.Response(200, request=request, json=True)

        if (
            url_path.endswith(("/message", "/prompt_async"))
            and request.method == "POST"
        ):
            return httpx.Response(204, request=request)

        if url_path == "/event" and request.method == "GET":
            lines: list[str] = []
            for evt in self.sse_events:
                lines.append(f"data: {json.dumps(evt)}")
                lines.append("")
            sse_content = "\n".join(lines) + "\n"

            async def stream_gen() -> AsyncIterator[bytes]:
                for line in sse_content.splitlines(keepends=True):
                    yield line.encode("utf-8")

            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                content=stream_gen(),
            )

        return httpx.Response(404, request=request, text="not found")


def _build_claude_provider(
    monkeypatch: pytest.MonkeyPatch, script: Sequence[Any]
) -> tuple[ClaudeAgentProvider, _FakeClaudeClient]:
    client = _FakeClaudeClient(script)

    def _client_factory(options: Any) -> _FakeClaudeClient:
        client.options = options
        return client

    monkeypatch.setattr(claude_module, "ClaudeSDKClient", _client_factory)
    return ClaudeAgentProvider(), client


@contextlib.asynccontextmanager
async def _mock_opencode_provider(
    sse_events: list[dict[str, Any]],
) -> AsyncIterator[tuple[OpenCodeProvider, _MockOpenCodeTransport]]:
    transport = _MockOpenCodeTransport(sse_events)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)
    try:
        yield provider, transport
    finally:
        await client.aclose()
        provider.stop()


# =========================================================================
# 1. 同一の標準タスクが両プロバイダで完走する (完了条件 2)
# =========================================================================


@pytest.mark.parametrize("task_id", ["consistency-pass", "voicing-fix"])
@pytest.mark.asyncio
async def test_standard_tasks_run_to_completion_on_both_providers(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch, task_id: str
) -> None:
    """標準タスク (consistency-pass, voicing-fix) が同一の AgentTask 定義の下で

    ClaudeAgentProvider と OpenCodeProvider の双方で完走することを検証する。
    """
    _seed_project(workspace_dir)
    task_def = get_task_definition(task_id)
    assert task_def is not None

    scope = {"part_id": "piano", "bars": (1, 2)} if task_def.requires_scope else None
    task = _make_task(workspace_dir, task_def, scope=scope)

    # Claude 側のモックスクリプト
    claude_script = [
        AssistantMessage(
            content=[TextBlock(text=f"Running {task_id}")],
            model="claude-3-7-sonnet",
            usage={"input_tokens": 200, "output_tokens": 50},
        ),
        ResultMessage(
            subtype="success",
            duration_ms=50,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s_claude",
            terminal_reason="completed",
            total_cost_usd=0.01,
            usage={"input_tokens": 200, "output_tokens": 50},
        ),
    ]
    claude_prov, _ = _build_claude_provider(monkeypatch, claude_script)

    # OpenCode 側のモックイベント列 (OpenCode SSE プロトコル準拠)
    opencode_events = [
        {"id": "evt_0", "type": "server.connected", "properties": {}},
        {
            "id": "evt_1",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_1",
                    "type": "text",
                    "text": f"Running {task_id}",
                },
            },
        },
        {
            "id": "evt_2",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_finish",
                    "type": "step-finish",
                    "tokens": {"input": 200, "output": 50},
                },
            },
        },
        {
            "id": "evt_3",
            "type": "session.idle",
            "properties": {"sessionID": "s_mock_parity"},
        },
    ]
    # 1. Claude プロバイダで実行
    claude_handle = await claude_prov.start(task)
    claude_events = [e async for e in claude_prov.stream(claude_handle.run_id)]
    claude_result = await claude_prov.result(claude_handle.run_id)

    assert claude_result.status == "completed"
    assert claude_events[-1].kind == "done"
    assert claude_result.usage.input_tokens == 200
    assert claude_result.usage.output_tokens == 50

    # 2. OpenCode プロバイダで同じタスクを実行
    # opencode は stdio なので mcp_servers の kind を stdio に設定
    opencode_task = AgentTask(
        task_type=task.task_type,
        project_id=task.project_id,
        prompt=task.prompt,
        workspace=task.workspace,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="stdio",
                config={
                    "workspace_dir": str(workspace_dir),
                    "run_id": "opencode_run_1",
                },
            )
        ],
        allowed_tools=task.allowed_tools,
        max_turns=task.max_turns,
        max_tokens_budget=task.max_tokens_budget,
        timeout_sec=task.timeout_sec,
    )
    async with _mock_opencode_provider(opencode_events) as (opencode_prov, _):
        opencode_handle = await opencode_prov.start(opencode_task)
        opencode_stream_events = [
            e async for e in opencode_prov.stream(opencode_handle.run_id)
        ]
        opencode_result = await opencode_prov.result(opencode_handle.run_id)

        assert opencode_result.status == "completed"
        assert opencode_stream_events[-1].kind == "done"
        assert opencode_result.usage.input_tokens == 200
        assert opencode_result.usage.output_tokens == 50


# =========================================================================
# 2. AgentEvent 系列の同値性・意味論パリティ
# =========================================================================


@pytest.mark.asyncio
async def test_agent_event_sequence_and_semantic_parity(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """思考(thinking) → ツール呼び出し(tool_use) → ツール結果(tool_result) →

    テキスト応答(text) → 完了(done) の標準シーケンスにおいて、
    両プロバイダが正規化する AgentEvent の系列が同じ種別順序と構造を持つことを検証する。
    """
    _seed_project(workspace_dir)
    task_def = get_task_definition("consistency-pass")
    assert task_def is not None
    task = _make_task(workspace_dir, task_def)

    tool_input = {"part": "piano", "bars": [1, 2]}
    tool_output = [{"id": 1, "midi": 60}]

    # Claude 側の対応メッセージ (UserMessage が ToolResultBlock を運ぶ規約)
    claude_script = [
        AssistantMessage(
            content=[
                ThinkingBlock(
                    thinking="Analyzing score structure", signature="sig_parity"
                ),
                ToolUseBlock(
                    id="call_1",
                    name="mcp__score__score_query",
                    input=tool_input,
                ),
            ],
            model="claude-3-7-sonnet",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="call_1",
                    content=json.dumps(tool_output),
                    is_error=False,
                ),
            ],
        ),
        AssistantMessage(
            content=[
                TextBlock(text="Found 1 note in range."),
            ],
            model="claude-3-7-sonnet",
        ),
        ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=100,
            is_error=False,
            num_turns=2,
            session_id="s1",
            terminal_reason="completed",
            usage={"input_tokens": 250, "output_tokens": 75},
        ),
    ]
    claude_prov, _ = _build_claude_provider(monkeypatch, claude_script)

    # OpenCode 側の対応イベント列 (OpenCode SSE プロトコル準拠)
    opencode_events = [
        {"id": "evt_0", "type": "server.connected", "properties": {}},
        {
            "id": "evt_1",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_think",
                    "type": "reasoning",
                    "text": "Analyzing score structure",
                    "time": {"start": 0, "end": 1},
                },
            },
        },
        {
            "id": "evt_2",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_tool",
                    "type": "tool",
                    "callID": "call_1",
                    "tool": "mcp__score__score_query",
                    "state": {
                        "status": "running",
                        "input": tool_input,
                    },
                },
            },
        },
        {
            "id": "evt_3",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_tool",
                    "type": "tool",
                    "callID": "call_1",
                    "tool": "mcp__score__score_query",
                    "state": {
                        "status": "completed",
                        "input": tool_input,
                        "output": json.dumps(tool_output),
                    },
                },
            },
        },
        {
            "id": "evt_4",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_text",
                    "type": "text",
                    "text": "Found 1 note in range.",
                    "time": {"start": 2, "end": 3},
                },
            },
        },
        {
            "id": "evt_5",
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s_mock_parity",
                "part": {
                    "id": "prt_finish",
                    "type": "step-finish",
                    "tokens": {"input": 250, "output": 75},
                },
            },
        },
        {
            "id": "evt_6",
            "type": "session.idle",
            "properties": {"sessionID": "s_mock_parity"},
        },
    ]
    # 両プロバイダを実行
    h_claude = await claude_prov.start(task)
    claude_events = [e async for e in claude_prov.stream(h_claude.run_id)]

    opencode_task = AgentTask(
        task_type=task.task_type,
        project_id=task.project_id,
        prompt=task.prompt,
        workspace=task.workspace,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="stdio",
                config={
                    "workspace_dir": str(workspace_dir),
                    "run_id": "opencode_parity_seq",
                },
            )
        ],
        allowed_tools=task.allowed_tools,
        max_turns=task.max_turns,
        max_tokens_budget=task.max_tokens_budget,
        timeout_sec=task.timeout_sec,
    )
    async with _mock_opencode_provider(opencode_events) as (opencode_prov, _):
        h_opencode = await opencode_prov.start(opencode_task)
        opencode_events_stream = [
            e async for e in opencode_prov.stream(h_opencode.run_id)
        ]

    # 1. イベント種別(kind)の系列順序が一致すること
    claude_kinds = [e.kind for e in claude_events]
    opencode_kinds = [e.kind for e in opencode_events_stream]

    expected_kinds = ["thinking", "tool_use", "tool_result", "text", "done"]
    assert claude_kinds == expected_kinds, f"Claude kinds mismatch: {claude_kinds}"
    assert opencode_kinds == expected_kinds, (
        f"OpenCode kinds mismatch: {opencode_kinds}"
    )

    # 2. シーケンス番号(seq)が両者ともに 0 始まりの単調増加であること
    assert [e.seq for e in claude_events] == list(range(5))
    assert [e.seq for e in opencode_events_stream] == list(range(5))

    # 3. ツール名が正しく設定されていること
    assert claude_events[1].tool_name == "mcp__score__score_query"
    assert opencode_events_stream[1].tool_name == "mcp__score__score_query"
    assert claude_events[2].tool_name == "mcp__score__score_query"
    assert opencode_events_stream[2].tool_name == "mcp__score__score_query"

    # 4. トークン消費量が両者ともに正しく集計されていること
    assert claude_events[-1].usage is not None
    assert claude_events[-1].usage.input_tokens == 250
    assert claude_events[-1].usage.output_tokens == 75

    assert opencode_events_stream[-1].usage is not None
    assert opencode_events_stream[-1].usage.input_tokens == 250
    assert opencode_events_stream[-1].usage.output_tokens == 75


# =========================================================================
# 3. ポリシー制約・セキュリティ境界のパリティ
# =========================================================================


def test_security_policy_enforcement_parity(workspace_dir: Path) -> None:
    """ネットワーク遮断 (WebFetch/WebSearch 禁止) およびツール権限制限が

    両プロバイダの設定生成時に対等に適用されていることを検証する(§8.5)。
    """
    # 1. Claude 側の静的ポリシー: disallowed_tools にネットワーク系が含まれる
    assert "WebFetch" in DISALLOWED_TOOLS
    assert "WebSearch" in DISALLOWED_TOOLS

    # 2. OpenCode 側の静的ポリシー: opencode.json の tools.webfetch が False
    opencode_tools_cfg = build_opencode_tools_config()
    assert opencode_tools_cfg.get("tools", {}).get("webfetch") is False

    # 3. OpenCode 用 opencode.json 生成時に反映されること
    task_def = get_task_definition("consistency-pass")
    assert task_def is not None
    task = _make_task(workspace_dir, task_def)
    opencode_cfg = generate_opencode_json(task, run_id="parity_run")

    assert opencode_cfg["tools"]["webfetch"] is False
    assert "score" in opencode_cfg["mcp"]


# =========================================================================
# 4. キャンセル動作と current.json 不変性のパリティ (完了条件 3)
# =========================================================================


@pytest.mark.asyncio
async def test_cancellation_preserves_current_score_on_both_providers(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """どちらのプロバイダで実行中にキャンセルしても、`current.json` が

    一切変更されず、結果ステータスが `cancelled` になることを検証する(M5c完了条件3)。
    """
    _seed_project(workspace_dir)
    current_path = storage.score_current_path(workspace_dir, _PROJECT_ID)
    current_bytes_before = current_path.read_bytes()

    task_def = get_task_definition("consistency-pass")
    assert task_def is not None
    task = _make_task(workspace_dir, task_def)

    # 1. Claude プロバイダのキャンセル
    claude_prov, _ = _build_claude_provider(monkeypatch, [])
    h_claude = await claude_prov.start(task)
    await claude_prov.cancel(h_claude.run_id)

    claude_events = [e async for e in claude_prov.stream(h_claude.run_id)]
    claude_res = await claude_prov.result(h_claude.run_id)

    assert claude_res.status == "cancelled"
    assert claude_events[-1].kind == "cancelled"
    assert current_path.read_bytes() == current_bytes_before

    # 2. OpenCode プロバイダのキャンセル
    opencode_task = AgentTask(
        task_type=task.task_type,
        project_id=task.project_id,
        prompt=task.prompt,
        workspace=task.workspace,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="stdio",
                config={
                    "workspace_dir": str(workspace_dir),
                    "run_id": "opencode_cancel_run",
                },
            )
        ],
        allowed_tools=task.allowed_tools,
        max_turns=task.max_turns,
        max_tokens_budget=task.max_tokens_budget,
        timeout_sec=task.timeout_sec,
    )
    async with _mock_opencode_provider([]) as (opencode_prov, _):
        h_opencode = await opencode_prov.start(opencode_task)
        await opencode_prov.cancel(h_opencode.run_id)

        opencode_events = [e async for e in opencode_prov.stream(h_opencode.run_id)]
        opencode_res = await opencode_prov.result(h_opencode.run_id)

        assert opencode_res.status == "cancelled"
        assert opencode_events[-1].kind == "cancelled"
        assert current_path.read_bytes() == current_bytes_before


# =========================================================================
# 5. CI での実行方法 (API キー・実バイナリが無い環境での skip 戦略)
# =========================================================================


@pytest.mark.slow
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY is not set; skipping live Claude provider E2E test",
)
@pytest.mark.asyncio
async def test_live_claude_provider_e2e(workspace_dir: Path) -> None:
    """実環境でのみ実行される ClaudeAgentProvider E2E スモークテスト。"""
    _seed_project(workspace_dir)
    task_def = get_task_definition("consistency-pass")
    assert task_def is not None
    task = _make_task(workspace_dir, task_def)

    provider = ClaudeAgentProvider()
    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]
    result = await provider.result(handle.run_id)

    assert result.status in ("completed", "failed", "truncated")
    assert len(events) > 0


@pytest.mark.slow
@pytest.mark.skipif(
    not is_opencode_available(),
    reason="opencode CLI binary not found; skipping live OpenCode provider E2E test",
)
@pytest.mark.asyncio
async def test_live_opencode_provider_e2e(workspace_dir: Path) -> None:
    """実環境でのみ実行される OpenCodeProvider E2E スモークテスト。"""
    _seed_project(workspace_dir)
    task_def = get_task_definition("consistency-pass")
    assert task_def is not None
    task = _make_task(workspace_dir, task_def)

    provider = OpenCodeProvider(workspace_dir=workspace_dir)
    try:
        handle = await provider.start(task)
        events = [e async for e in provider.stream(handle.run_id)]
        result = await provider.result(handle.run_id)

        assert result.status in ("completed", "failed", "truncated")
        assert len(events) > 0
    finally:
        provider.stop()
