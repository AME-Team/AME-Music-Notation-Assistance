"""#52: OpenCodeProvider (`app/agent/providers/opencode.py`) のテスト。

OpenCode サーバ(`opencode serve`)のライフサイクル管理(R-13)、
`opencode.json`生成、`ProviderContractTests`契約準拠、
イベント正規化(`AgentEvent`)、タイムアウト/キャンセル/予算超過、
リトライ方針を検証する。

CI環境(opencode未インストール/ネットワーク無し)でも決定論的に完走するよう、
`httpx.MockTransport`を用いてHTTP通信をモックする。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_contract import ProviderContractTests
from app.agent.provider import AgentTask, McpServerSpec
from app.agent.providers import opencode as opencode_module
from app.agent.providers.opencode import (
    OpenCodeProvider,
    OpenCodeProviderError,
    OpenCodeServer,
    OpenCodeServerNotFoundError,
    OpenCodeServerStartError,
    generate_opencode_json,
    is_opencode_available,
    resolve_opencode_path,
)


def _make_sse_body(events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for evt in events:
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    return "\n".join(lines) + "\n"


class _MockOpenCodeTransport(httpx.AsyncBaseTransport):
    """`opencode serve` のREST/SSE APIを模倣するテスト用トランスポート。"""

    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        session_id: str = "ses_mock123",
        fail_session_create_times: int = 0,
    ) -> None:
        self.session_id = session_id
        self.fail_session_create_times = fail_session_create_times
        self.session_created_count = 0
        self.abort_called = False
        self.messages_sent: list[dict[str, Any]] = []
        if events is None:
            self.events = [
                {"id": "evt_0", "type": "server.connected", "properties": {}},
                {
                    "id": "evt_1",
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": self.session_id,
                        "part": {
                            "id": "prt_1",
                            "type": "text",
                            "text": "done analyzing",
                            "time": {"start": 0, "end": 1},
                        },
                    },
                },
                {
                    "id": "evt_2",
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": self.session_id,
                        "part": {
                            "id": "prt_finish",
                            "type": "step-finish",
                            "tokens": {"input": 50, "output": 10},
                        },
                    },
                },
                {
                    "id": "evt_3",
                    "type": "session.idle",
                    "properties": {"sessionID": self.session_id},
                },
            ]
        else:
            self.events = events

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_path = request.url.path

        if url_path == "/session" and request.method == "POST":
            self.session_created_count += 1
            if self.session_created_count <= self.fail_session_create_times:
                return httpx.Response(500, request=request, text="server error")
            return httpx.Response(
                200,
                request=request,
                json={"id": self.session_id, "title": "test"},
            )

        if url_path.endswith("/abort") and request.method == "POST":
            self.abort_called = True
            return httpx.Response(200, request=request, json=True)

        if (
            url_path.endswith(("/message", "/prompt_async"))
            and request.method == "POST"
        ):
            body = json.loads(request.content.decode("utf-8"))
            self.messages_sent.append(body)
            return httpx.Response(204, request=request)

        if url_path == "/event" and request.method == "GET":
            sse_content = _make_sse_body(self.events)

            async def stream_generator() -> AsyncIterator[bytes]:
                for line in sse_content.splitlines(keepends=True):
                    yield line.encode("utf-8")

            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                content=stream_generator(),
            )

        return httpx.Response(404, request=request, text="not found")


def _make_mock_provider(
    *,
    events: list[dict[str, Any]] | None = None,
    fail_session_create_times: int = 0,
) -> tuple[OpenCodeProvider, _MockOpenCodeTransport]:
    transport = _MockOpenCodeTransport(
        events=events,
        fail_session_create_times=fail_session_create_times,
    )
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)
    return provider, transport


# =========================================================================
# 1. プロバイダ契約テスト (ProviderContractTests)
# =========================================================================


class TestOpenCodeProviderContract(ProviderContractTests):
    def provider(self) -> OpenCodeProvider:
        prov, _ = _make_mock_provider()
        return prov


# =========================================================================
# 2. イベント正規化テスト
# =========================================================================


@pytest.mark.anyio
async def test_opencode_normalizes_thinking_text_and_tool_events(
    tmp_path: Path,
) -> None:
    session_id = "ses_norm"
    events = [
        {"id": "evt_0", "type": "server.connected", "properties": {}},
        {
            "id": "evt_1",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_think",
                    "type": "reasoning",
                    "text": "thinking about notes",
                    "time": {"start": 0, "end": 1},
                },
            },
        },
        {
            "id": "evt_2",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_tool",
                    "type": "tool",
                    "callID": "call_1",
                    "tool": "score_apply_ops",
                    "state": {
                        "status": "running",
                        "input": {"ops": [{"op": "set_pitch"}]},
                    },
                },
            },
        },
        {
            "id": "evt_3",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_tool",
                    "type": "tool",
                    "callID": "call_1",
                    "tool": "score_apply_ops",
                    "state": {
                        "status": "completed",
                        "input": {"ops": [{"op": "set_pitch"}]},
                        "output": json.dumps({"ok": True, "violations": []}),
                    },
                },
            },
        },
        {
            "id": "evt_4",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_text",
                    "type": "text",
                    "text": "pitch adjusted successfully",
                    "time": {"start": 1, "end": 2},
                },
            },
        },
        {
            "id": "evt_5",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_finish",
                    "type": "step-finish",
                    "tokens": {
                        "input": 120,
                        "output": 35,
                        "cache": {"read": 10, "write": 5},
                    },
                },
            },
        },
        {
            "id": "evt_6",
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        },
    ]

    transport = _MockOpenCodeTransport(events=events, session_id=session_id)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)

    task = AgentTask(
        task_type="consistency-pass",
        project_id="prj_1",
        prompt="fix notes",
        workspace=tmp_path,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="stdio",
                config={"workspace_dir": str(tmp_path), "run_id": "run_norm"},
            )
        ],
    )

    handle = await provider.start(task)
    assert handle.run_id == "run_norm"

    collected = [e async for e in provider.stream(handle.run_id)]
    kinds = [e.kind for e in collected]
    assert kinds == ["thinking", "tool_use", "tool_result", "text", "done"]

    # 思考ブロックの検証
    assert collected[0].payload["text"] == "thinking about notes"
    # ツール呼び出しの検証
    assert collected[1].tool_name == "score_apply_ops"
    assert collected[1].payload["input"] == {"ops": [{"op": "set_pitch"}]}
    # ツール結果の検証
    assert collected[2].tool_name == "score_apply_ops"
    assert collected[2].payload["output"] == {"ok": True, "violations": []}
    # テキスト出力の検証
    assert collected[3].payload["text"] == "pitch adjusted successfully"
    # 終端イベントの検証
    assert collected[4].payload["status"] == "completed"
    assert collected[4].payload["staged_ops_count"] == 1

    # result()の検証
    res = await provider.result(handle.run_id)
    assert res.status == "completed"
    assert res.staged_ops_count == 1
    assert res.turns == 1
    assert res.usage.input_tokens == 120
    assert res.usage.output_tokens == 35
    assert res.usage.cache_read_input_tokens == 10
    assert res.usage.cache_creation_input_tokens == 5


@pytest.mark.anyio
async def test_opencode_handles_tool_error(tmp_path: Path) -> None:
    session_id = "ses_err"
    events = [
        {"id": "evt_0", "type": "server.connected", "properties": {}},
        {
            "id": "evt_1",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_tool",
                    "type": "tool",
                    "callID": "call_err",
                    "tool": "score_apply_ops",
                    "state": {"status": "error", "error": "Invalid operation"},
                },
            },
        },
        {
            "id": "evt_2",
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        },
    ]

    transport = _MockOpenCodeTransport(events=events, session_id=session_id)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)

    task = AgentTask(
        task_type="test",
        project_id="prj_1",
        prompt="run",
        workspace=tmp_path,
    )
    handle = await provider.start(task)
    collected = [e async for e in provider.stream(handle.run_id)]

    assert [e.kind for e in collected] == ["tool_result", "done"]
    assert collected[0].payload["is_error"] is True
    assert collected[0].payload["output"] == "Invalid operation"


@pytest.mark.anyio
async def test_opencode_session_error_results_in_failed(tmp_path: Path) -> None:
    session_id = "ses_fail"
    events = [
        {"id": "evt_0", "type": "server.connected", "properties": {}},
        {
            "id": "evt_1",
            "type": "session.error",
            "properties": {
                "sessionID": session_id,
                "error": {"name": "APIError", "message": "model unavailable"},
            },
        },
    ]

    transport = _MockOpenCodeTransport(events=events, session_id=session_id)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)

    task = AgentTask(
        task_type="test",
        project_id="prj_1",
        prompt="run",
        workspace=tmp_path,
    )
    handle = await provider.start(task)
    collected = [e async for e in provider.stream(handle.run_id)]

    assert [e.kind for e in collected] == ["error"]
    assert collected[0].payload["status"] == "failed"

    res = await provider.result(handle.run_id)
    assert res.status == "failed"
    assert "APIError" in (res.error or "")


# =========================================================================
# 3. タイムアウト・予算超過・キャンセルテスト
# =========================================================================


@pytest.mark.anyio
async def test_opencode_budget_exceeded_results_in_truncated(tmp_path: Path) -> None:
    session_id = "ses_budget"
    events = [
        {"id": "evt_0", "type": "server.connected", "properties": {}},
        {
            "id": "evt_1",
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "id": "prt_step",
                    "type": "step-finish",
                    "tokens": {"input": 600, "output": 500},
                },
            },
        },
        {
            "id": "evt_2",
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        },
    ]

    transport = _MockOpenCodeTransport(events=events, session_id=session_id)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)

    task = AgentTask(
        task_type="test",
        project_id="prj_1",
        prompt="run",
        workspace=tmp_path,
        max_tokens_budget=1000,  # 600 + 500 = 1100 > 1000
    )
    handle = await provider.start(task)
    collected = [e async for e in provider.stream(handle.run_id)]

    assert [e.kind for e in collected] == ["done"]
    assert collected[0].payload["status"] == "truncated"
    assert transport.abort_called is True

    res = await provider.result(handle.run_id)
    assert res.status == "truncated"


@pytest.mark.anyio
async def test_opencode_cancel_during_stream_calls_abort(tmp_path: Path) -> None:
    session_id = "ses_cancel"

    class _HangingTransport(_MockOpenCodeTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/event":

                async def infinite_stream() -> AsyncIterator[bytes]:
                    yield b'data: {"id": "evt_0", "type": "server.connected", "properties": {}}\n\n'
                    # 最初の思考イベントを流すことでストリーム受信用ループを動かす
                    yield b'data: {"id": "evt_think", "type": "message.part.updated", "properties": {"sessionID": "ses_cancel", "part": {"id": "p0", "type": "reasoning", "text": "thinking", "time": {"start": 0, "end": 1}}}}\n\n'
                    # キャンセル要求が届くまで待機
                    while not self.abort_called:
                        await asyncio.sleep(0.02)
                    yield b'data: {"id": "evt_1", "type": "session.error", "properties": {"sessionID": "ses_cancel", "error": {"name": "MessageAbortedError"}}}\n\n'

                return httpx.Response(
                    200,
                    request=request,
                    headers={"content-type": "text/event-stream"},
                    content=infinite_stream(),
                )
            return await super().handle_async_request(request)

    transport = _HangingTransport(session_id=session_id)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-opencode")
    provider = OpenCodeProvider(base_url="http://mock-opencode", http_client=client)

    task = AgentTask(
        task_type="test",
        project_id="prj_1",
        prompt="run",
        workspace=tmp_path,
        timeout_sec=5,
    )
    handle = await provider.start(task)

    async def _stream_and_cancel() -> list[Any]:
        events = []
        async for evt in provider.stream(handle.run_id):
            events.append(evt)
            if evt.kind != "cancelled":
                await provider.cancel(handle.run_id)
        return events

    collected = await _stream_and_cancel()
    assert transport.abort_called is True
    assert collected[-1].kind == "cancelled"

    res = await provider.result(handle.run_id)
    assert res.status == "cancelled"


# =========================================================================
# 4. リトライ方針テスト (§8.9)
# =========================================================================


@pytest.mark.anyio
async def test_opencode_retries_session_creation_up_to_3_times(tmp_path: Path) -> None:
    # 2回失敗して3回目で成功
    provider, transport = _make_mock_provider(fail_session_create_times=2)
    task = AgentTask(
        task_type="test",
        project_id="prj_1",
        prompt="run",
        workspace=tmp_path,
        timeout_sec=5,
    )
    handle = await provider.start(task)
    collected = [e async for e in provider.stream(handle.run_id)]

    assert transport.session_created_count == 3
    assert collected[-1].kind == "done"
    assert collected[-1].payload["status"] == "completed"


@pytest.mark.anyio
async def test_opencode_fails_after_max_retries_exceeded(tmp_path: Path) -> None:
    # 4回失敗(上限3回を超える)
    provider, transport = _make_mock_provider(fail_session_create_times=4)
    task = AgentTask(
        task_type="test",
        project_id="prj_1",
        prompt="run",
        workspace=tmp_path,
        timeout_sec=5,
    )
    handle = await provider.start(task)
    collected = [e async for e in provider.stream(handle.run_id)]

    assert transport.session_created_count == 3
    assert collected[-1].kind == "error"
    assert collected[-1].payload["status"] == "failed"


# =========================================================================
# 5. opencode.json 生成と構成テスト
# =========================================================================


def test_generate_opencode_json_creates_valid_file(tmp_path: Path) -> None:
    workspace = tmp_path / "agent_ws"
    workspace.mkdir()

    task = AgentTask(
        task_type="consistency-pass",
        project_id="proj_99",
        prompt="fix score",
        workspace=workspace,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="stdio",
                config={"workspace_dir": str(tmp_path), "run_id": "run_cfg_123"},
            )
        ],
    )

    cfg = generate_opencode_json(task, "run_cfg_123")
    json_path = workspace / "opencode.json"
    assert json_path.is_file()

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved == cfg

    score_mcp = cfg["mcp"]["score"]
    assert score_mcp["type"] == "local"
    assert score_mcp["environment"]["AME_PROJECT_ID"] == "proj_99"
    assert score_mcp["environment"]["AME_RUN_ID"] == "run_cfg_123"
    assert score_mcp["environment"]["AME_WORKSPACE_DIR"] == str(tmp_path)
    assert "PYTHONPATH" in score_mcp["environment"]
    assert "cwd" in score_mcp

    # toolsセクションの検証(policy.py連携)
    tools = cfg["tools"]
    assert tools.get("bash") is True
    assert tools.get("write") is True
    assert tools.get("edit") is True
    assert tools.get("webfetch") is False


def test_generate_opencode_json_raises_on_missing_workspace_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "agent_ws"
    workspace.mkdir()

    task = AgentTask(
        task_type="test",
        project_id="proj_1",
        prompt="prompt",
        workspace=workspace,
        mcp_servers=[McpServerSpec(name="score", kind="stdio", config={})],
    )

    with pytest.raises(
        OpenCodeProviderError, match="missing required config key 'workspace_dir'"
    ):
        generate_opencode_json(task, "run_1")


@pytest.mark.anyio
async def test_opencode_start_rejects_duplicate_run_id(tmp_path: Path) -> None:
    provider, _ = _make_mock_provider()
    task = AgentTask(
        task_type="test",
        project_id="proj_1",
        prompt="prompt",
        workspace=tmp_path,
        mcp_servers=[
            McpServerSpec(
                name="score",
                kind="stdio",
                config={"workspace_dir": str(tmp_path), "run_id": "fixed_run_id"},
            )
        ],
    )

    await provider.start(task)
    with pytest.raises(OpenCodeProviderError, match="is already in use"):
        await provider.start(task)


# =========================================================================
# 6. サーバのライフサイクル管理テスト (R-13)
# =========================================================================


def test_opencode_server_cleanup_stale_process(tmp_path: Path) -> None:
    server = OpenCodeServer(workspace_dir=tmp_path)
    state_file = tmp_path / ".opencode_server.json"

    # 存在しない架空のPIDを記録しておく
    dead_pid = 999999
    state_file.write_text(
        json.dumps({"pid": dead_pid, "port": 12345}), encoding="utf-8"
    )

    server.cleanup_stale_process()
    assert not state_file.exists(), "stale state file should be removed"


def test_is_opencode_process_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    # 正常ケース: cmdlineにopencodeが含まれる場合
    class _FakePath:
        def __init__(self, content: str, exists: bool = True) -> None:
            self._content = content
            self._exists = exists

        def exists(self) -> bool:
            return self._exists

        def read_text(self, *args: Any, **kwargs: Any) -> str:
            return self._content

    # opencodeプロセスの場合
    monkeypatch.setattr(
        opencode_module, "Path", lambda p: _FakePath("opencode serve --port 0")
    )
    assert opencode_module._is_opencode_process(1234) is True

    # 無関係なプロセスの場合(PID再利用事故防止)
    monkeypatch.setattr(
        opencode_module, "Path", lambda p: _FakePath("python -m pytest")
    )
    assert opencode_module._is_opencode_process(5678) is False

    # プロセスが存在しない場合
    monkeypatch.setattr(
        opencode_module,
        "Path",
        lambda p: _FakePath("", exists=False) if "/proc/" in str(p) else Path(p),
    )
    monkeypatch.setattr(
        opencode_module.subprocess,
        "run",
        lambda *args, **kwargs: type("Res", (), {"stdout": ""})(),
    )
    assert opencode_module._is_opencode_process(999999) is False


def test_cleanup_stale_process_does_not_kill_unrelated_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(workspace_dir=tmp_path)
    state_file = tmp_path / ".opencode_server.json"
    state_file.write_text(json.dumps({"pid": 1111, "port": 12345}), encoding="utf-8")

    terminated: list[int] = []
    monkeypatch.setattr(server, "_terminate_pid", lambda pid: terminated.append(pid))
    # PID 1111はopencodeではないとする
    monkeypatch.setattr(opencode_module, "_is_opencode_process", lambda pid: False)

    server.cleanup_stale_process()

    assert terminated == [], "_terminate_pid must NOT be called on unrelated process"
    assert not state_file.exists(), "stale state file should still be cleaned up"


def test_cleanup_stale_process_terminates_verified_opencode_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(workspace_dir=tmp_path)
    state_file = tmp_path / ".opencode_server.json"
    state_file.write_text(json.dumps({"pid": 2222, "port": 12345}), encoding="utf-8")

    terminated: list[int] = []
    monkeypatch.setattr(server, "_terminate_pid", lambda pid: terminated.append(pid))
    # PID 2222はopencodeプロセスと判定
    monkeypatch.setattr(opencode_module, "_is_opencode_process", lambda pid: True)

    server.cleanup_stale_process()

    assert terminated == [2222], (
        "_terminate_pid must be called for verified opencode PID"
    )
    assert not state_file.exists()


@pytest.mark.asyncio
async def test_get_base_url_runs_in_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(workspace_dir=tmp_path)
    monkeypatch.setattr(server, "ensure_running", lambda: "http://127.0.0.1:4567")
    provider = OpenCodeProvider(server=server)

    # _get_base_url が非同期コンテキストでブロックせず正常に取得できること
    url = await provider._get_base_url()
    assert url == "http://127.0.0.1:4567"


def test_opencode_server_start_raises_when_binary_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_module, "resolve_opencode_path", lambda *_: None)
    server = OpenCodeServer(workspace_dir=tmp_path, bin_path=None)

    with pytest.raises(
        OpenCodeServerNotFoundError, match="opencode CLI binary not found"
    ):
        server.start()


def test_opencode_server_start_raises_on_premature_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 起動直後に即座に非0終了するフェイクプロセス
    import subprocess

    class _FailingProc:
        pid = 1234
        returncode = 1

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(
        opencode_module, "resolve_opencode_path", lambda *_: Path("/fake/opencode")
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FailingProc())

    server = OpenCodeServer(workspace_dir=tmp_path)
    with pytest.raises(
        OpenCodeServerStartError, match="opencode serve exited prematurely"
    ):
        server.start()


def test_opencode_server_stop_removes_state_file_and_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(workspace_dir=tmp_path)
    state_file = tmp_path / ".opencode_server.json"
    state_file.write_text(json.dumps({"pid": 12345, "port": 5000}), encoding="utf-8")

    terminated: list[int] = []
    monkeypatch.setattr(server, "_terminate_pid", lambda pid: terminated.append(pid))

    class _FakeRunningProc:
        pid = 12345

        def poll(self) -> None:
            return None

        def wait(self, timeout: float = 1.0) -> None:
            pass

    server._proc = _FakeRunningProc()  # type: ignore[assignment]
    server.stop()
    assert not state_file.exists()
    assert terminated == [12345], "stop must call _terminate_pid to kill process tree"
    assert server._proc is None


def test_resolve_opencode_path_respects_custom_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_bin = tmp_path / "custom_opencode"
    custom_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    custom_bin.chmod(0o755)

    assert resolve_opencode_path(custom_bin) == custom_bin

    monkeypatch.setenv("AME_OPENCODE_PATH", str(custom_bin))
    assert resolve_opencode_path() == custom_bin


def test_is_opencode_available_returns_bool() -> None:
    result = is_opencode_available()
    assert isinstance(result, bool)
