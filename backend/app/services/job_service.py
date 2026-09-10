"""Job Manager + SSE 進捗配信(#13)。

Redis/Celery は導入しない(§5.1)。SQLite のジョブテーブル + プロセス間通知
(子プロセスの stdout を JSON Lines として読む)で単一ユーザー向けには十分。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.infra import db, ids, proc

VALID_STAGES = {"dummy", "separate", "beat", "transcribe", "quantize"}

# `python -m app.worker.dsp_main` を確実に解決するため、呼び出し元の CWD に関わらず
# backend/ を明示的に子プロセスの cwd にする(app/services/job_service.py から2階層上)。
_BACKEND_DIR = Path(__file__).resolve().parents[2]


class JobNotFoundError(LookupError):
    pass


class UnknownStageError(ValueError):
    pass


@dataclass
class JobManager:
    workspace_dir: Path
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)
    _subscribers: dict[str, list[asyncio.Queue]] = field(default_factory=dict)
    _cancel_requested: set[str] = field(default_factory=set)

    def _conn(self) -> sqlite3.Connection:
        return db.get_connection(self.workspace_dir / "db.sqlite3")

    async def create_job(self, *, project_id: str, stage: str, params: dict) -> str:
        if stage not in VALID_STAGES:
            raise UnknownStageError(stage)

        job_id = ids.new_id("job")
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO jobs(id, project_id, stage, status, progress, message, "
                "params_json, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', 0.0, NULL, ?, ?, ?)",
                (job_id, project_id, stage, json.dumps(params), now, now),
            )
            conn.commit()

        asyncio.create_task(self._run_job(job_id, project_id, stage, params))
        return job_id

    def get_job(self, job_id: str) -> dict:
        row = self._conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return row

    async def cancel_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return  # 既に終了しているジョブへの後追いキャンセルは無視する
        self._cancel_requested.add(job_id)
        process = self._processes.get(job_id)
        if process is not None and process.returncode is None:
            proc.kill_process_tree(process.pid)
        await self._update_job(job_id, status="cancelled")
        last_progress = self.get_job(job_id)["progress"]
        await self._publish(
            job_id, {"job_id": job_id, "status": "cancelled", "progress": last_progress}
        )
        await self._close_subscribers(job_id)

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """SSE購読用のキューを新規登録する。既に完了しているジョブなら現在状態を1件流して閉じる。"""
        queue: asyncio.Queue = asyncio.Queue()
        job = self.get_job(job_id)
        if job["status"] in ("succeeded", "failed", "cancelled"):
            queue.put_nowait(
                {"job_id": job_id, "status": job["status"], "progress": job["progress"]}
            )
            queue.put_nowait(None)
        else:
            self._subscribers.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id)
        if subs and queue in subs:
            subs.remove(queue)

    async def _publish(self, job_id: str, event: dict) -> None:
        for queue in self._subscribers.get(job_id, []):
            await queue.put(event)

    async def _close_subscribers(self, job_id: str) -> None:
        for queue in self._subscribers.get(job_id, []):
            await queue.put(None)
        self._subscribers.pop(job_id, None)

    async def _update_job(self, job_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(UTC).isoformat()
        columns = ", ".join(f"{k} = ?" for k in fields)
        async with self._write_lock:
            conn = self._conn()
            conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))
            conn.commit()

    async def _run_job(self, job_id: str, project_id: str, stage: str, params: dict) -> None:
        # #13/Windows実機で発覚したレース: create_job() が asyncio.create_task で
        # このコルーチンをスケジュールした直後に cancel_job() が呼ばれると、
        # このコルーチンが実行され始める前にジョブは既に "cancelled" 確定済みのことがある。
        if job_id in self._cancel_requested:
            self._cancel_requested.discard(job_id)
            return

        await self._update_job(job_id, status="running")
        await self._publish(job_id, {"job_id": job_id, "status": "running", "progress": 0.0})

        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            # #16/#18: Worker は別プロセスのため、JobManager が実際に使っている
            # workspace_dir(テストでは tmp_path)を明示的に伝える(config.resolve_workspace_dir参照)。
            "AME_WORKSPACE_DIR": str(self.workspace_dir),
        }
        cmd = [
            sys.executable,
            "-m",
            "app.worker.dsp_main",
            job_id,
            project_id,
            stage,
            json.dumps(params),
        ]
        process = await proc.spawn_json_lines_worker(cmd, env=env, cwd=str(_BACKEND_DIR))
        self._processes[job_id] = process

        # spawn の最中に cancel_job() が来ていた場合(_processes 未登録でkillできなかった)、
        # ここで追いかけてkillする。
        if job_id in self._cancel_requested and process.returncode is None:
            proc.kill_process_tree(process.pid)

        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # status は更新しない: cancel_job() が並行して "cancelled" を書き込んでいる
            # 可能性があり、ここで "running" に上書きすると誤って failed 扱いになる
            # (Windows実機で実際に踏んだレース)。
            await self._update_job(
                job_id,
                progress=event.get("progress", 0.0),
                message=event.get("message"),
            )
            await self._publish(job_id, {"job_id": job_id, "status": "running", **event})

        exit_code = await process.wait()
        self._processes.pop(job_id, None)
        self._cancel_requested.discard(job_id)

        current = self.get_job(job_id)
        if current["status"] == "cancelled":
            return  # cancel_job が既に最終状態・購読者クローズを行っている

        if exit_code == 0:
            await self._update_job(job_id, status="succeeded", progress=1.0, exit_code=exit_code)
            await self._publish(job_id, {"job_id": job_id, "status": "succeeded", "progress": 1.0})
        else:
            # NFR-04: Worker がクラッシュしてもジョブは failed として記録され、
            # API サーバ(このプロセス)は生き続ける。
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            await self._update_job(
                job_id,
                status="failed",
                message=stderr.decode("utf-8", errors="replace")[-500:],
                exit_code=exit_code,
            )
            last_progress = self.get_job(job_id)["progress"]
            await self._publish(
                job_id, {"job_id": job_id, "status": "failed", "progress": last_progress}
            )
        await self._close_subscribers(job_id)
