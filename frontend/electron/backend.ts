import { type ChildProcess, spawn } from "node:child_process";
import { readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { app } from "electron";
import treeKill from "tree-kill";
import { generateToken, getFreePort } from "./port";

/**
 * Python バックエンドの spawn / 監視 / 終了(#77)。
 * OpenCode サーバ管理(R-13)と同じ方針: PID/ポートを記録し、起動時に前回の
 * 残骸プロセスを検出して掃除する。ツリーごとの kill には `tree-kill` を使う
 * (Windows では `taskkill /T /F` 相当、POSIX ではプロセスグループへ送信する)。
 */

interface LockInfo {
  pid: number;
  port: number;
}

function lockPath(): string {
  return path.join(app.getPath("userData"), "backend.lock.json");
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

function killTree(pid: number): Promise<void> {
  return new Promise((resolve) => treeKill(pid, () => resolve()));
}

async function cleanupStaleBackend(): Promise<void> {
  try {
    const raw = await readFile(lockPath(), "utf-8");
    const info = JSON.parse(raw) as LockInfo;
    if (isAlive(info.pid)) {
      await killTree(info.pid);
    }
  } catch {
    // ロックファイルが無い/読めない = 掃除対象なし
  } finally {
    await rm(lockPath(), { force: true });
  }
}

export type BackendStatus = "starting" | "ready" | "error";

export interface BackendHandle {
  baseUrl: string;
  token: string;
  stop(): Promise<void>;
}

// dev: <repo>/frontend/dist-electron/backend.js -> <repo>/backend
// packaged: 同梱方式は Q-17(#82) で M6 までに決定する。M0 時点では同一の相対解決を既定とする。
function backendDir(): string {
  return path.resolve(__dirname, "..", "..", "backend");
}

async function waitForHealth(
  baseUrl: string,
  child: ChildProcess,
  timeoutMs = 30000,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) return false;
    try {
      const resp = await fetch(`${baseUrl}/health`);
      if (resp.ok) return true;
    } catch {
      // まだ起動していない
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

export async function startBackend(
  onStatus: (status: BackendStatus, detail?: string) => void,
): Promise<BackendHandle> {
  await cleanupStaleBackend();

  const port = await getFreePort();
  const token = generateToken();
  onStatus("starting");

  const dir = backendDir();
  const child = spawn(
    "uv",
    ["run", "--project", dir, "python", "-m", "app.main", "--port", String(port)],
    {
      cwd: dir,
      env: {
        ...process.env,
        AME_BACKEND_PORT: String(port),
        AME_BACKEND_TOKEN: token,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
      },
      detached: process.platform !== "win32",
    },
  );

  if (child.pid) {
    await writeFile(lockPath(), JSON.stringify({ pid: child.pid, port }), "utf-8");
  }

  child.stderr?.on("data", (chunk: Buffer) => {
    console.error(`[backend] ${chunk.toString("utf-8")}`);
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  const ready = await waitForHealth(baseUrl, child);
  if (!ready) {
    onStatus("error", "backend failed to start (health check timed out)");
  } else {
    onStatus("ready");
  }

  child.on("exit", (code) => {
    if (code !== 0 && code !== null) {
      onStatus("error", `backend exited unexpectedly (code ${code})`);
    }
  });

  return {
    baseUrl,
    token,
    stop: async () => {
      if (child.pid) await killTree(child.pid);
      await rm(lockPath(), { force: true });
    },
  };
}
