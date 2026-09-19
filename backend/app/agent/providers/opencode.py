"""OpenCodeProvider(#52, 設計書§8.2/§8.3/§8.5/§8.9/§15 R-13)— L2の代替プロバイダ。

OpenCode に公式 Python SDK は存在しないため(v0.4で確認済み、TS/JS SDKのみ)、
`opencode serve`を外部サーバとして起動し`httpx`でHTTPエンドポイントを直接
叩く(§8.3)。

【サーバのライフサイクル管理(R-13)】
- ポートは動的割り当て(`--port 0 --hostname 127.0.0.1`)。
- PID とポートを記録し(`workspace_dir / ".opencode_server.json"`)、
  起動時に前回の残骸を検出して掃除する。
- アプリ終了時に確実に停止する(`atexit`およびFastAPI lifespan連携)。
- サーバ起動失敗時はClaudeAgentProviderへの自動切替はせず、ユーザーに尋ねる
  (§8.9「OpenCodeサーバ起動失敗: ClaudeAgentProviderにフォールバックするか尋ねる
  (自動切替はしない)」)。

【MCPと権限(§8.4/§8.5)】
- OpenCode側はstdio子プロセスとして`score-mcp`(`mcp/stdio_server.py`, #44)を起動する。
- 各runの開始時に`task.workspace / "opencode.json"`を生成する。
- 権限制御は`agent/policy.py`の`build_opencode_tools_config()`(#45)と連携する。
- 公開run_idを`McpServerSpec.config["run_id"]`から取得して採用し、
  `score_apply_ops`のステージング先ファイル名を承認フローと一致させる(#50)。

【障害時の挙動(§8.9)】
- プロバイダ API エラー: 指数バックオフでリトライ(3回)。
- ターン/トークン/時間の上限到達: run を `truncated` として終了。
- ユーザーによるキャンセル: `POST /session/:id/abort`を呼び、ステージング破棄。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from app.agent.mcp.stdio_server import opencode_mcp_config
from app.agent.policy import (
    build_opencode_tools_config,
    exceeds_token_budget,
)
from app.agent.provider import (
    AgentEvent,
    AgentEventKind,
    AgentResult,
    AgentRunHandle,
    AgentRunNotFoundError,
    AgentRunStatus,
    AgentTask,
    TokenUsage,
)

_MAX_CONNECT_ATTEMPTS: Final = 3
_BACKOFF_BASE_SEC: Final = 1.0
_ENV_OPENCODE_PATH: Final = "AME_OPENCODE_PATH"
_DEFAULT_HOST: Final = "127.0.0.1"
_SERVER_STATE_FILENAME: Final = ".opencode_server.json"
_SERVER_LOG_FILENAME: Final = "opencode_server.log"


class OpenCodeProviderError(RuntimeError):
    """OpenCodeProvider の基底例外。"""


class OpenCodeServerNotFoundError(OpenCodeProviderError):
    """`opencode` CLI バイナリが見つからない場合。"""


class OpenCodeServerStartError(OpenCodeProviderError):
    """`opencode serve` サーバの起動に失敗した場合(§8.9)。"""


def resolve_opencode_path(custom_path: Path | str | None = None) -> Path | None:
    """`opencode` CLIバイナリのパスを解決する。

    環境変数 `AME_OPENCODE_PATH`、PATH上の検索、一般的なインストール先
    (`~/.opencode/bin/`, `~/.local/bin/`) を順に探す。
    """
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return p

    env_path = os.environ.get(_ENV_OPENCODE_PATH)
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    found = shutil.which("opencode")
    if found:
        return Path(found)

    exe_name = "opencode.exe" if sys.platform == "win32" else "opencode"
    cmd_name = "opencode.cmd" if sys.platform == "win32" else "opencode"
    candidates = [
        Path.home() / ".opencode" / "bin" / exe_name,
        Path.home() / ".opencode" / "bin" / cmd_name,
        Path.home() / ".local" / "bin" / exe_name,
    ]
    for c in candidates:
        if c.is_file():
            return c

    return None


def is_opencode_available() -> bool:
    """`opencode` CLIが実行可能か(プロバイダ一覧APIのconfigured判定)。"""
    bin_path = resolve_opencode_path()
    if bin_path is None:
        return False
    try:
        proc = subprocess.run(
            [str(bin_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _resolve_run_id(task: AgentTask) -> str | None:
    """`McpServerSpec.config["run_id"]`があれば採用する(#50の設計契約)。"""
    for spec in task.mcp_servers:
        if spec.name == "score":
            run_id = spec.config.get("run_id")
            if run_id:
                return str(run_id)
    return None


def _token_usage_from_tokens_dict(tokens: dict[str, Any] | None) -> TokenUsage:
    tokens = tokens or {}
    cache = tokens.get("cache") or {}
    return TokenUsage(
        input_tokens=int(tokens.get("input") or 0),
        output_tokens=int(tokens.get("output") or 0),
        cache_read_input_tokens=int(cache.get("read") or 0),
        cache_creation_input_tokens=int(cache.get("write") or 0),
    )


def _extract_tool_result_payload(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def _resolve_interrupted_status(run: _RunState, *, default: AgentRunStatus) -> AgentRunStatus:
    if run.cancel_requested:
        return "cancelled"
    if run.budget_exceeded:
        return "truncated"
    return default


def generate_opencode_json(task: AgentTask, run_id: str) -> dict[str, Any]:
    """各run用の`opencode.json`設定を生成し、`task.workspace / "opencode.json"`へ書き出す。"""
    mcp_servers: dict[str, Any] = {}
    backend_dir = Path(__file__).resolve().parents[3]

    for spec in task.mcp_servers:
        if spec.name == "score":
            workspace_dir = spec.config.get("workspace_dir")
            if not workspace_dir:
                raise OpenCodeProviderError(
                    f"mcp server spec {spec.name!r} is missing required config key 'workspace_dir'"
                )
            score_cfg = opencode_mcp_config(
                project_id=task.project_id,
                run_id=run_id,
                workspace_dir=Path(workspace_dir),
            )
            # 子プロセスがbackendパッケージをimportできるようcwdとPYTHONPATHを設定する
            if "score" in score_cfg:
                score_cfg["score"]["cwd"] = str(backend_dir)
                env = score_cfg["score"].get("environment", {})
                env["PYTHONPATH"] = str(backend_dir)
                score_cfg["score"]["environment"] = env
            mcp_servers.update(score_cfg)

    tools_config = build_opencode_tools_config()
    opencode_cfg = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": mcp_servers,
        "tools": tools_config.get("tools", {}),
    }

    config_path = task.workspace / "opencode.json"
    config_path.write_text(json.dumps(opencode_cfg, indent=2), encoding="utf-8")
    return opencode_cfg


def _is_opencode_process(pid: int) -> bool:
    """PIDが実際にopencodeプロセスであるかを検証する(R-13)。

    OSによるPID再利用で無関係な別プロセスを誤ってkillする事故を防ぐ。
    """
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            return "opencode" in res.stdout.lower()
        except Exception:
            return False

    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            cmdline = cmdline_path.read_text(encoding="utf-8", errors="replace")
            return "opencode" in cmdline
        except Exception:
            return False

    # /proc が存在しないPOSIX環境(macOS等)のフォールバック
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        return "opencode" in res.stdout.lower()
    except Exception:
        return False


class OpenCodeServer:
    """`opencode serve` プロセスのライフサイクルマネージャ(R-13)。

    - PID とポートを記録し、起動時に前回の残骸を検出して掃除する。
    - ポートは動的割り当て(`--port 0`)。
    - アプリ終了時に確実に停止する。
    """

    def __init__(
        self,
        *,
        workspace_dir: Path,
        bin_path: Path | None = None,
        host: str = _DEFAULT_HOST,
        startup_timeout: float = 15.0,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.bin_path = bin_path
        self.host = host
        self.startup_timeout = startup_timeout
        self.state_path = workspace_dir / _SERVER_STATE_FILENAME
        self.log_path = workspace_dir / _SERVER_LOG_FILENAME

        self._proc: subprocess.Popen[str] | None = None
        self._base_url: str | None = None
        self._port: int | None = None
        self._pid: int | None = None
        self._lock = threading.Lock()

        # プロセス終了時の確実な停止
        atexit.register(self.stop)

    @property
    def base_url(self) -> str | None:
        return self._base_url

    def cleanup_stale_process(self) -> None:
        """前回の残骸(PIDファイル)を検出して掃除する(R-13)。"""
        if not self.state_path.exists():
            return
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            pid = data.get("pid")
            if isinstance(pid, int) and pid > 0 and _is_opencode_process(pid):
                self._terminate_pid(pid)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                self.state_path.unlink()

    def _terminate_pid(self, pid: int) -> None:
        """PIDおよびその子プロセスツリー(score-mcp等)を確実に停止する(R-13)。"""
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5.0,
                    check=False,
                )
            return

        try:
            os.kill(pid, 0)
        except OSError:
            return  # プロセスは存在しない

        # POSIX: 子プロセス(score-mcp)の孤児化を防ぐためプロセスグループへシグナルを送る
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid

        # 自身のプロセスグループを誤爆しないよう保護
        use_pgid = pgid > 1 and pgid != os.getpgrp()

        def _send_signal(sig: int) -> None:
            if use_pgid:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)

        try:
            _send_signal(signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            # まだ生きていればSIGKILL
            _send_signal(signal.SIGKILL)
        except OSError:
            pass

    def is_running(self) -> bool:
        with self._lock:
            if self._proc is not None:
                return self._proc.poll() is None
            return False

    def start(self) -> str:
        """`opencode serve` を起動し、リッスンURLを返す。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._base_url:
                return self._base_url

            self.cleanup_stale_process()

            bin_path = self.bin_path or resolve_opencode_path()
            if bin_path is None:
                raise OpenCodeServerNotFoundError(
                    "opencode CLI binary not found. "
                    "Please install opencode or set AME_OPENCODE_PATH."
                )

            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(self.log_path, "w", encoding="utf-8")  # noqa: SIM115

            cmd = [
                str(bin_path),
                "serve",
                "--port",
                "0",
                "--hostname",
                self.host,
            ]

            popen_kwargs: dict[str, Any] = {
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
                "text": True,
                "cwd": str(self.workspace_dir),
            }
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True

            try:
                self._proc = subprocess.Popen(cmd, **popen_kwargs)
            except Exception as exc:
                log_file.close()
                raise OpenCodeServerStartError(f"failed to spawn opencode serve: {exc}") from exc

            # リッスンURLの出力をログファイルから待機
            deadline = time.monotonic() + self.startup_timeout
            url: str | None = None
            port: int | None = None

            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    log_file.close()
                    out = ""
                    with contextlib.suppress(Exception):
                        out = self.log_path.read_text(encoding="utf-8")
                    raise OpenCodeServerStartError(
                        f"opencode serve exited prematurely with code {self._proc.returncode}: "
                        f"{out}"
                    )

                if self.log_path.exists():
                    try:
                        content = self.log_path.read_text(encoding="utf-8")
                        m = re.search(r"listening on (http://(?:\S+?):(\d+))", content)
                        if m:
                            url = m.group(1)
                            port = int(m.group(2))
                            break
                    except Exception:
                        pass
                time.sleep(0.1)

            if not url or not port:
                self.stop()
                raise OpenCodeServerStartError(
                    f"opencode serve timed out after {self.startup_timeout}s waiting to listen"
                )

            self._base_url = url
            self._port = port
            self._pid = self._proc.pid

            # PIDとポートを記録する(R-13)
            state_data = {
                "pid": self._pid,
                "port": self._port,
                "base_url": self._base_url,
                "started_at": time.time(),
            }
            with contextlib.suppress(Exception):
                self.state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

            return self._base_url

    def stop(self) -> None:
        """サーバプロセスおよびその子プロセスを停止し、残骸を掃除する。"""
        with self._lock:
            if self._proc is not None:
                pid = self._proc.pid
                self._terminate_pid(pid)
                with contextlib.suppress(Exception):
                    self._proc.wait(timeout=1.0)
                self._proc = None

            with contextlib.suppress(Exception):
                if self.state_path.exists():
                    self.state_path.unlink()

            self._base_url = None
            self._port = None
            self._pid = None

    def ensure_running(self) -> str:
        if self.is_running() and self._base_url:
            return self._base_url
        return self.start()


@dataclass
class _RunState:
    run_id: str
    task: AgentTask
    session_id: str | None = None
    status: AgentRunStatus = "running"
    events: list[AgentEvent] | None = None
    cancel_requested: bool = False
    budget_exceeded: bool = False
    turns: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    staged_ops_count: int = 0
    error: str | None = None


class OpenCodeProvider:
    """`AgentProvider` Protocolの実装(#52, 設計書§8.2/§8.3)。

    OpenCode サーバ(`opencode serve`)と `httpx` で通信し、セッション作成・
    SSE イベント購読・プロンプト送信を行う。
    """

    name = "opencode"

    def __init__(
        self,
        *,
        workspace_dir: Path | None = None,
        server: OpenCodeServer | None = None,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.server = server
        if self.server is None and workspace_dir is not None and base_url is None:
            self.server = OpenCodeServer(workspace_dir=workspace_dir)
        self.base_url = base_url
        self._http_client = http_client
        self._runs: dict[str, _RunState] = {}

    def stop(self) -> None:
        """管理下のサーバプロセスを停止する。"""
        if self.server is not None:
            self.server.stop()

    async def _get_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        if self.server:
            # サーバ起動失敗時はフォールバックせず明示的な例外を送出する(§8.9)
            return await asyncio.to_thread(self.server.ensure_running)
        raise OpenCodeProviderError("No server or base_url configured for OpenCodeProvider")

    async def start(self, task: AgentTask) -> AgentRunHandle:
        run_id = _resolve_run_id(task) or str(uuid.uuid4())
        if run_id in self._runs:
            raise OpenCodeProviderError(
                f"run_id {run_id!r} is already in use; McpServerSpec.config['run_id'] "
                "must be unique per run (the caller is responsible for uniqueness)"
            )

        # runごとの opencode.json を生成する(#52, §8.4)
        generate_opencode_json(task, run_id)

        self._runs[run_id] = _RunState(run_id=run_id, task=task)
        return AgentRunHandle(run_id=run_id)

    async def stream(self, run_id: str) -> AsyncIterator[AgentEvent]:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)

        if run.events is not None:
            for event in run.events:
                yield event
            return

        events: list[AgentEvent] = []
        seq = 0

        def _next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq - 1

        if run.cancel_requested:
            event = AgentEvent(
                run_id=run_id, seq=_next_seq(), kind="cancelled", payload={"message": "cancelled"}
            )
            events.append(event)
            run.status = "cancelled"
            run.events = events
            yield event
            return

        deadline = time.monotonic() + run.task.timeout_sec

        try:
            async for event in self._drive(run, events, _next_seq, deadline):
                yield event
        except Exception as exc:
            event = self._terminal_event(run, _next_seq(), status="failed", error=str(exc))
            events.append(event)
            run.events = events
            yield event
        else:
            run.events = events

    async def _drive(
        self,
        run: _RunState,
        events: list[AgentEvent],
        next_seq: Callable[[], int],
        deadline: float,
    ) -> AsyncIterator[AgentEvent]:
        attempt = 0
        received_any_message = False

        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = _resolve_interrupted_status(run, default="truncated")
                event = self._terminal_event(run, next_seq(), status=status, error=None)
                events.append(event)
                yield event
                return

            try:
                base_url = await self._get_base_url()
            except Exception as exc:
                # サーバ起動失敗時はリトライせず即時失敗(§8.9)
                event = self._terminal_event(run, next_seq(), status="failed", error=str(exc))
                events.append(event)
                yield event
                return

            client = (
                self._http_client
                if self._http_client is not None
                else httpx.AsyncClient(base_url=base_url, timeout=30.0)
            )

            try:
                async with client if self._http_client is None else contextlib.nullcontext(client):
                    # 1. セッション作成
                    session_params: dict[str, Any] = {"title": run.task.task_type}
                    # ヘッドレス実行でブロックしないよう全権限を許可する
                    session_params["permission"] = [
                        {"permission": "*", "pattern": "*", "action": "allow"}
                    ]

                    try:
                        session_resp = await asyncio.wait_for(
                            client.post(
                                "/session",
                                json=session_params,
                                params={"directory": str(run.task.workspace)},
                            ),
                            timeout=max(remaining, 0.01),
                        )
                        session_resp.raise_for_status()
                    except (httpx.HTTPError, TimeoutError) as exc:
                        if attempt >= _MAX_CONNECT_ATTEMPTS or (deadline - time.monotonic()) <= 0:
                            status = "truncated" if (deadline - time.monotonic()) <= 0 else "failed"
                            event = self._terminal_event(
                                run, next_seq(), status=status, error=str(exc)
                            )
                            events.append(event)
                            yield event
                            return
                        backoff = min(
                            _BACKOFF_BASE_SEC * (2 ** (attempt - 1)),
                            max(deadline - time.monotonic(), 0.01),
                        )
                        await asyncio.sleep(backoff)
                        continue

                    session_data = session_resp.json()
                    session_id = session_data["id"]
                    run.session_id = session_id

                    if run.cancel_requested:
                        with contextlib.suppress(Exception):
                            await client.post(
                                f"/session/{session_id}/abort",
                                params={"directory": str(run.task.workspace)},
                            )
                        event = self._terminal_event(
                            run, next_seq(), status="cancelled", error=None
                        )
                        events.append(event)
                        yield event
                        return

                    # 2. SSE イベントストリームを開く
                    stream_params = {"directory": str(run.task.workspace)}
                    async with client.stream("GET", "/event", params=stream_params) as stream_resp:
                        # 3. プロンプトメッセージを送信
                        message_body: dict[str, Any] = {
                            "parts": [{"type": "text", "text": run.task.prompt}]
                        }
                        if run.task.model and "/" in run.task.model:
                            p_id, m_id = run.task.model.split("/", 1)
                            message_body["model"] = {"providerID": p_id, "modelID": m_id}

                        # prompt_async または message を送信する
                        try:
                            msg_resp = await client.post(
                                f"/session/{session_id}/prompt_async",
                                json=message_body,
                                params={"directory": str(run.task.workspace)},
                            )
                            if msg_resp.status_code == 404:
                                # prompt_async が未サポートの古い版へのフォールバック
                                asyncio.create_task(
                                    client.post(
                                        f"/session/{session_id}/message",
                                        json=message_body,
                                        params={"directory": str(run.task.workspace)},
                                    )
                                )
                        except Exception:
                            # message 送信を試みる
                            asyncio.create_task(
                                client.post(
                                    f"/session/{session_id}/message",
                                    json=message_body,
                                    params={"directory": str(run.task.workspace)},
                                )
                            )

                        seen_tool_calls: set[str] = set()
                        seen_tool_results: set[str] = set()
                        emitted_parts: set[str] = set()
                        cached_parts: dict[str, dict[str, Any]] = {}

                        lines_iter = stream_resp.aiter_lines()
                        while True:
                            if run.cancel_requested:
                                with contextlib.suppress(Exception):
                                    await client.post(
                                        f"/session/{session_id}/abort",
                                        params={"directory": str(run.task.workspace)},
                                    )

                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                status = _resolve_interrupted_status(run, default="truncated")
                                event = self._terminal_event(
                                    run, next_seq(), status=status, error=None
                                )
                                events.append(event)
                                yield event
                                return

                            try:
                                line = await asyncio.wait_for(
                                    lines_iter.__anext__(), timeout=max(remaining, 0.01)
                                )
                            except StopAsyncIteration:
                                break
                            except TimeoutError:
                                status = _resolve_interrupted_status(run, default="truncated")
                                event = self._terminal_event(
                                    run, next_seq(), status=status, error=None
                                )
                                events.append(event)
                                yield event
                                return

                            if not line or not line.startswith("data: "):
                                continue

                            received_any_message = True
                            raw_data = line[6:].strip()
                            if not raw_data:
                                continue

                            try:
                                event_dict = json.loads(raw_data)
                            except json.JSONDecodeError:
                                continue

                            event_type = event_dict.get("type")
                            props = event_dict.get("properties") or {}

                            # 該当セッション以外のイベントはスキップ
                            evt_sid = props.get("sessionID")
                            if evt_sid and evt_sid != session_id:
                                continue

                            # イベント正規化
                            norm_events = self._normalize_event(
                                run,
                                event_type,
                                props,
                                next_seq,
                                seen_tool_calls,
                                seen_tool_results,
                                emitted_parts,
                                cached_parts,
                            )
                            for out_evt in norm_events:
                                events.append(out_evt)
                                yield out_evt

                            # トークン予算の超過チェック
                            total_tokens = run.usage.input_tokens + run.usage.output_tokens
                            if exceeds_token_budget(total_tokens, run.task.max_tokens_budget):
                                run.budget_exceeded = True
                                with contextlib.suppress(Exception):
                                    await client.post(
                                        f"/session/{session_id}/abort",
                                        params={"directory": str(run.task.workspace)},
                                    )

                            # 終端イベントの判定
                            if event_type in ("session.idle", "session.error") or (
                                event_type == "session.status"
                                and isinstance(props.get("status"), dict)
                                and props.get("status", {}).get("type") == "idle"
                            ):
                                # 未完了だったテキスト・思考パートをフラッシュする
                                for pid, p in cached_parts.items():
                                    if pid not in emitted_parts:
                                        emitted_parts.add(pid)
                                        ptype = p.get("type")
                                        ptext = p.get("text") or ""
                                        if ptype == "reasoning" and ptext:
                                            e = AgentEvent(
                                                run_id=run.run_id,
                                                seq=next_seq(),
                                                kind="thinking",
                                                payload={"text": ptext},
                                            )
                                            events.append(e)
                                            yield e
                                        elif ptype == "text" and ptext:
                                            e = AgentEvent(
                                                run_id=run.run_id,
                                                seq=next_seq(),
                                                kind="text",
                                                payload={"text": ptext},
                                            )
                                            events.append(e)
                                            yield e

                                if event_type == "session.error":
                                    err_info = props.get("error") or {}
                                    err_name = (
                                        err_info.get("name")
                                        if isinstance(err_info, dict)
                                        else str(err_info)
                                    )
                                    if err_name == "MessageAbortedError":
                                        status = _resolve_interrupted_status(
                                            run, default="cancelled"
                                        )
                                        error_msg = None
                                    else:
                                        status = "failed"
                                        error_msg = str(err_info)
                                else:
                                    status = _resolve_interrupted_status(run, default="completed")
                                    error_msg = None

                                event = self._terminal_event(
                                    run, next_seq(), status=status, error=error_msg
                                )
                                events.append(event)
                                yield event
                                return

                    # ストリーム終了時(フォールバック)
                    fallback_status = _resolve_interrupted_status(run, default="completed")
                    event = self._terminal_event(
                        run, next_seq(), status=fallback_status, error=None
                    )
                    events.append(event)
                    yield event
                    return

            except (httpx.HTTPError, TimeoutError) as exc:
                if received_any_message:
                    event = self._terminal_event(run, next_seq(), status="failed", error=str(exc))
                    events.append(event)
                    yield event
                    return

                if run.cancel_requested:
                    event = self._terminal_event(run, next_seq(), status="cancelled", error=None)
                    events.append(event)
                    yield event
                    return

                remaining = deadline - time.monotonic()
                if attempt >= _MAX_CONNECT_ATTEMPTS or remaining <= 0:
                    status = "truncated" if remaining <= 0 else "failed"
                    event = self._terminal_event(run, next_seq(), status=status, error=str(exc))
                    events.append(event)
                    yield event
                    return

                await asyncio.sleep(min(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), remaining))
                continue

    def _normalize_event(
        self,
        run: _RunState,
        event_type: str | None,
        props: dict[str, Any],
        next_seq: Callable[[], int],
        seen_tool_calls: set[str],
        seen_tool_results: set[str],
        emitted_parts: set[str],
        cached_parts: dict[str, dict[str, Any]],
    ) -> list[AgentEvent]:
        out: list[AgentEvent] = []

        if event_type == "message.part.updated":
            part = props.get("part") or {}
            part_id = part.get("id") or ""
            part_type = part.get("type")
            cached_parts[part_id] = part

            if part_type == "reasoning":
                # time.end があるか、または既に終了したパート
                time_info = part.get("time") or {}
                if time_info.get("end") is not None and part_id not in emitted_parts:
                    emitted_parts.add(part_id)
                    text = part.get("text") or ""
                    if text:
                        out.append(
                            AgentEvent(
                                run_id=run.run_id,
                                seq=next_seq(),
                                kind="thinking",
                                payload={"text": text},
                            )
                        )

            elif part_type == "text":
                time_info = part.get("time") or {}
                if time_info.get("end") is not None and part_id not in emitted_parts:
                    emitted_parts.add(part_id)
                    text = part.get("text") or ""
                    if text:
                        out.append(
                            AgentEvent(
                                run_id=run.run_id,
                                seq=next_seq(),
                                kind="text",
                                payload={"text": text},
                            )
                        )

            elif part_type == "tool":
                tool_name = part.get("tool") or ""
                call_id = part.get("callID") or part_id
                state = part.get("state") or {}
                status = state.get("status")

                if status in ("pending", "running") and call_id not in seen_tool_calls:
                    seen_tool_calls.add(call_id)
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="tool_use",
                            tool_name=tool_name,
                            payload={"input": state.get("input", {}), "tool_use_id": call_id},
                        )
                    )

                elif status == "completed" and call_id not in seen_tool_results:
                    seen_tool_results.add(call_id)
                    output = _extract_tool_result_payload(state.get("output"))
                    is_ok_op = (
                        "score_apply_ops" in tool_name
                        and isinstance(output, dict)
                        and output.get("ok") is True
                    )
                    if is_ok_op:
                        run.staged_ops_count += 1
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="tool_result",
                            tool_name=tool_name,
                            payload={"output": output, "is_error": False},
                        )
                    )

                elif status == "error" and call_id not in seen_tool_results:
                    seen_tool_results.add(call_id)
                    out.append(
                        AgentEvent(
                            run_id=run.run_id,
                            seq=next_seq(),
                            kind="tool_result",
                            tool_name=tool_name,
                            payload={"output": state.get("error"), "is_error": True},
                        )
                    )

            elif part_type == "step-finish":
                run.turns += 1
                tokens = part.get("tokens") or {}
                run.usage = _token_usage_from_tokens_dict(tokens)

        elif event_type == "message.updated":
            info = props.get("info") or {}
            tokens = info.get("tokens") or {}
            if tokens:
                run.usage = _token_usage_from_tokens_dict(tokens)

        return out

    def _terminal_event(
        self, run: _RunState, seq: int, *, status: AgentRunStatus, error: str | None
    ) -> AgentEvent:
        run.status = status
        run.error = error
        status_to_kind: dict[AgentRunStatus, AgentEventKind] = {
            "completed": "done",
            "truncated": "done",
            "failed": "error",
            "cancelled": "cancelled",
        }
        kind = status_to_kind[status]
        payload: dict[str, Any] = {"status": status, "staged_ops_count": run.staged_ops_count}
        if error is not None:
            payload["error"] = error
        return AgentEvent(run_id=run.run_id, seq=seq, kind=kind, payload=payload, usage=run.usage)

    async def cancel(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        run.cancel_requested = True
        if run.status == "running":
            run.status = "cancelled"

        if run.session_id:
            try:
                base_url = await self._get_base_url()
                client = (
                    self._http_client
                    if self._http_client is not None
                    else httpx.AsyncClient(base_url=base_url, timeout=5.0)
                )
                async with client if self._http_client is None else contextlib.nullcontext(client):
                    await client.post(
                        f"/session/{run.session_id}/abort",
                        params={"directory": str(run.task.workspace)},
                    )
            except Exception:
                pass

    async def result(self, run_id: str) -> AgentResult:
        run = self._runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        return AgentResult(
            run_id=run_id,
            status=run.status,
            turns=run.turns,
            usage=run.usage,
            staged_ops_count=run.staged_ops_count,
            error=run.error,
        )
