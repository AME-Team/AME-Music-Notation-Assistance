import { type ChildProcess, spawn } from "node:child_process";
import { readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { app } from "electron";
import treeKill from "tree-kill";
import { logger } from "./logger";
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

interface BackendCommand {
  command: string;
  args: string[];
  cwd: string;
  /** ffmpeg同梱ディレクトリ等、子プロセスのPATH先頭へ追加するパス(#62/#80)。 */
  extraPathDirs: string[];
}

// dev: <repo>/frontend/dist-electron/backend.js -> <repo>/backend を `uv run` で起動する
// (開発者のマシンに `uv`/Python 3.12 が導入済みであることを前提とする)。
// packaged: Q-17(#80で決定)により、`scripts/prepare-python-runtime.mjs` が
// ビルド前に組み立てた embeddable Python + 事前インストール済み依存一式を
// `resources/python-runtime` として同梱し(`electron-builder.yml`の
// `extraResources`)、その `python.exe` を直接起動する。エンドユーザーの
// マシンにPython/uvは一切不要になる(#62完了条件)。
function resolveBackendCommand(port: number): BackendCommand {
  if (app.isPackaged) {
    const runtimeDir = path.join(process.resourcesPath, "python-runtime");
    const ffmpegDir = path.join(process.resourcesPath, "ffmpeg");
    return {
      command: path.join(runtimeDir, "python.exe"),
      args: ["-m", "app.main", "--port", String(port)],
      cwd: runtimeDir,
      extraPathDirs: [ffmpegDir],
    };
  }

  const dir = path.resolve(__dirname, "..", "..", "backend");
  return {
    command: "uv",
    args: ["run", "--project", dir, "python", "-m", "app.main", "--port", String(port)],
    cwd: dir,
    extraPathDirs: [],
  };
}

/**
 * `extraPathDirs`をPATH環境変数の先頭へ追加した環境変数オブジェクトを返す(#62)。
 *
 * Windowsでは環境変数名が大文字小文字を区別せず、`process.env`は通常
 * `Path`というキーで持つ(`PATH`ではない)。単純に`{ ...process.env, PATH: ... }`
 * とすると`Path`と`PATH`が別キーとして両方残ってしまい、子プロセスへ渡る
 * 環境ブロックでどちらが有効になるか不定になる(#62 Gate1レビュー指摘)。
 * 既存のPATHキー(大文字小文字を問わず)を削除してから単一のキーで設定する。
 */
function withExtraPathDirs(env: NodeJS.ProcessEnv, extraPathDirs: string[]): NodeJS.ProcessEnv {
  if (extraPathDirs.length === 0) return { ...env };

  const result: NodeJS.ProcessEnv = {};
  let existingPath = "";
  for (const [key, value] of Object.entries(env)) {
    if (key.toUpperCase() === "PATH") {
      existingPath = value ?? "";
      continue;
    }
    result[key] = value;
  }

  const pathSeparator = process.platform === "win32" ? ";" : ":";
  result.PATH = [...extraPathDirs, existingPath].join(pathSeparator);
  return result;
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

  const { command, args, cwd, extraPathDirs } = resolveBackendCommand(port);
  const child = spawn(command, args, {
    cwd,
    env: {
      ...withExtraPathDirs(process.env, extraPathDirs),
      AME_BACKEND_PORT: String(port),
      AME_BACKEND_TOKEN: token,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    },
    detached: process.platform !== "win32",
  });

  if (child.pid) {
    await writeFile(lockPath(), JSON.stringify({ pid: child.pid, port }), "utf-8");
  }

  child.stdout?.on("data", (chunk: Buffer) => {
    const text = chunk.toString("utf-8").trimEnd();
    if (text) logger.info("backend", text);
  });

  child.stderr?.on("data", (chunk: Buffer) => {
    const text = chunk.toString("utf-8").trimEnd();
    if (text) logger.warn("backend", text);
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  logger.info("backend", `Waiting for backend health check at ${baseUrl}...`);
  const ready = await waitForHealth(baseUrl, child);
  if (!ready) {
    logger.error("backend", "Backend failed to start (health check timed out)");
    onStatus("error", "backend failed to start (health check timed out)");
  } else {
    logger.info("backend", "Backend is healthy and ready");
    onStatus("ready");
  }

  // アプリ終了時に stop() が意図的に kill した場合は "error" 通知しない。
  // このフラグが無いと、終了処理で BrowserWindow が既に破棄された後に
  // webContents.send() を呼ぼうとして "Object has been destroyed" で
  // メインプロセスがクラッシュする(#77)。
  let stopping = false;

  child.on("exit", (code) => {
    if (!stopping && code !== 0 && code !== null) {
      onStatus("error", `backend exited unexpectedly (code ${code})`);
    }
  });

  return {
    baseUrl,
    token,
    stop: async () => {
      stopping = true;
      if (child.pid) await killTree(child.pid);
      await rm(lockPath(), { force: true });
    },
  };
}
