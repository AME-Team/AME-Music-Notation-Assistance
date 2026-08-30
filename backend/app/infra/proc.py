"""クロスプラットフォームな子プロセス起動・ツリーkill(#13, #77, #79)。

DSP Worker は API サーバとは別プロセスで実行する(NFR-04)。プロセスツリーごと
確実に終了できる必要があるのは Windows(#77 と同じ問題: R-13)。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys


def subprocess_kwargs() -> dict:
    """`asyncio.create_subprocess_exec` に渡す、プロセスグループ生成のためのkwargs。

    POSIX: 新しいセッションを作り、`kill_process_tree` で `killpg` できるようにする。
    Windows: 新しいプロセスグループを作り、`taskkill /T` でツリーごと終了できるようにする。
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int, *, timeout: float = 5.0) -> None:
    """プロセスとその子プロセスをツリーごと終了する。

    #77/#79: Windows では単純な `process.terminate()` は子プロセスを残しうるため、
    `taskkill /T /F` でツリーごと終了する。POSIX ではプロセスグループへ shutdown する。
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def is_process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return pid > 0 and _posix_alive_via_permission_error(pid)
    return True


def _posix_alive_via_permission_error(pid: int) -> bool:
    # os.kill が PermissionError を投げるのは「存在はするが権限が無い」場合。
    # ProcessLookupError は「存在しない」場合のみ。呼び出し元で区別する。
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def spawn_json_lines_worker(
    cmd: list[str], *, env: dict[str, str], cwd: str | None = None
) -> asyncio.subprocess.Process:
    """stdout を JSON Lines として非同期に読み取れる子プロセスを起動する(#13)。

    `cwd` は呼び出し元プロセスの実行時カレントディレクトリに関わらず、
    `python -m app.worker.dsp_main` が確実に `app` パッケージを解決できるよう
    明示的に指定する(未指定だと親プロセスの CWD を継承し、リポジトリ直下から
    起動された場合に `ModuleNotFoundError` でワーカーが即クラッシュする)。
    """
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        **subprocess_kwargs(),
    )
