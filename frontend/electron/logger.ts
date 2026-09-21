import { appendFile, mkdir, readdir, stat, unlink } from "node:fs/promises";
import path from "node:path";
import { app, type WebContents } from "electron";
import { type LogLevel, normalizeLogLevel, shouldLogDidFailLoad } from "./logLevel";

/**
 * ログ管理モジュール。
 * - エラーログ、デバッグログ、情報ログを日付別ファイル (app-YYYY-MM-DD.log) に記録。
 * - ログ保存期間は 1 週間 (7 日間)。古いログファイルは自動削除して肥大化を防止。
 * - メインプロセス、レンダラープロセス、バックエンドプロセスの全出力を集約。
 */

export type { LogLevel } from "./logLevel";

const RETENTION_DAYS = 7;
const RETENTION_MS = RETENTION_DAYS * 24 * 60 * 60 * 1000;

export function getLogsDir(): string {
  return path.join(app.getPath("userData"), "logs");
}

function getLogFileName(date: Date = new Date()): string {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  return `app-${yyyy}-${mm}-${dd}.log`;
}

/**
 * 保存期間 (7日) を超過した古いログファイルを削除する。
 */
export async function cleanupOldLogs(): Promise<void> {
  try {
    const logsDir = getLogsDir();
    const files = await readdir(logsDir);
    const now = Date.now();

    for (const file of files) {
      if (!file.endsWith(".log")) continue;
      const filePath = path.join(logsDir, file);
      try {
        const fileStat = await stat(filePath);
        if (now - fileStat.mtimeMs > RETENTION_MS) {
          await unlink(filePath);
          console.log(`[logger] Removed expired log file (> 7 days): ${file}`);
        }
      } catch {
        // ファイルアクセスの失敗は無視
      }
    }
  } catch {
    // ディレクトリ未作成時などの失敗は無視
  }
}

let isInitialized = false;

export async function initLogger(): Promise<void> {
  if (isInitialized) return;
  try {
    await mkdir(getLogsDir(), { recursive: true });
    await cleanupOldLogs();
    isInitialized = true;
  } catch (err) {
    console.error("[logger] Failed to initialize logs directory:", err);
  }
}

function formatMessage(level: LogLevel, source: string, message: string, args: unknown[]): string {
  const ts = new Date().toISOString();
  let extra = "";
  if (args.length > 0) {
    extra =
      " " +
      args
        .map((a) => {
          if (a instanceof Error) return a.stack || a.message;
          if (typeof a === "object") {
            try {
              return JSON.stringify(a);
            } catch {
              return String(a);
            }
          }
          return String(a);
        })
        .join(" ");
  }
  return `[${ts}] [${level}] [${source}] ${message}${extra}\n`;
}

function writeLog(line: string): void {
  const logPath = path.join(getLogsDir(), getLogFileName());
  // 非同期で追記する (エラーはコンソールに出力して落とさない)
  void appendFile(logPath, line, "utf-8").catch((err) => {
    console.error("[logger] Failed to write to log file:", err);
  });
}

export const logger = {
  debug(source: string, message: string, ...args: unknown[]): void {
    const line = formatMessage("DEBUG", source, message, args);
    console.debug(`[${source}] ${message}`, ...args);
    writeLog(line);
  },
  info(source: string, message: string, ...args: unknown[]): void {
    const line = formatMessage("INFO", source, message, args);
    console.log(`[${source}] ${message}`, ...args);
    writeLog(line);
  },
  warn(source: string, message: string, ...args: unknown[]): void {
    const line = formatMessage("WARN", source, message, args);
    console.warn(`[${source}] ${message}`, ...args);
    writeLog(line);
  },
  error(source: string, message: string, ...args: unknown[]): void {
    const line = formatMessage("ERROR", source, message, args);
    console.error(`[${source}] ${message}`, ...args);
    writeLog(line);
  },
};

/**
 * レンダラープロセスのコンソールログおよびクラッシュイベントをログファイルに集約する。
 */
export function attachWebContentsLogger(webContents: WebContents): void {
  // Electron 35+ は (event, details) 形式(#148: 旧形式は非推奨の警告が出ていたため
  // 両対応にする)。
  webContents.on(
    "console-message",
    (
      _event: unknown,
      levelOrDetails:
        | number
        | { level?: string; message?: string; lineNumber?: number; sourceId?: string },
      legacyMessage?: string,
      legacyLine?: number,
      legacySourceId?: string,
    ) => {
      const details =
        typeof levelOrDetails === "object" && levelOrDetails !== null ? levelOrDetails : null;
      const rawMessage = details ? (details.message ?? "") : (legacyMessage ?? "");
      const line = details ? (details.lineNumber ?? 0) : (legacyLine ?? 0);
      const sourceId = details ? (details.sourceId ?? "") : (legacySourceId ?? "");
      const levelName = details
        ? (details.level ?? "info").toLowerCase()
        : (["verbose", "info", "warning", "error", "verbose"][levelOrDetails as number] ?? "info");
      const loc = sourceId ? ` (${sourceId}:${line})` : "";
      const msg = `${rawMessage}${loc}`;
      switch (levelName) {
        case "verbose":
        case "debug":
          logger.debug("renderer", msg);
          break;
        case "warning":
        case "warn":
          logger.warn("renderer", msg);
          break;
        case "error":
          logger.error("renderer", msg);
          break;
        default:
          logger.info("renderer", msg);
      }
    },
  );

  webContents.on("render-process-gone", (_event, details) => {
    logger.error(
      "renderer",
      `Render process gone: reason=${details.reason}, exitCode=${details.exitCode}`,
    );
  });
}

/**
 * ウィンドウ/レンダラー側の失敗を漏れなくログに記録する(#148)。
 *
 * `console-message`だけでは「preloadスクリプトの読み込み失敗」「ページ読み込み失敗」
 * 「レンダラープロセスの異常終了」が**ログに一切残らない**ため、原因究明が
 * ログだけでは不可能だった(Windows実機で実際に困った)。
 */
export function attachFatalErrorHandlers(webContents: WebContents): void {
  // preload の評価エラー(preload側の例外はレンダラーのconsoleにも出ない)。
  webContents.on("preload-error", (_event, preloadPath, error) => {
    logger.error("preload", `Failed to load ${preloadPath}:`, error);
  });

  // ページ読み込み失敗(ERR_CONNECTION_REFUSED等。開発サーバの落ちなど)。    // サブフレームの失敗・中断(ERR_ABORTED)は通常動作でも出るため除外する
  // (#148レビュー指摘: 無条件に記録すると本当のエラーが埋もれる)。
  webContents.on(
    "did-fail-load",
    (
      _event,
      errorCode: number,
      errorDescription: string,
      validatedURL: string,
      isMainFrame?: boolean,
    ) => {
      if (!shouldLogDidFailLoad({ errorCode, isMainFrame })) return;
      logger.error(
        "renderer",
        `Failed to load ${validatedURL} (${errorCode}: ${errorDescription})`,
      );
    },
  );
}

/**
 * メインプロセスの子プロセス(レンダラー/GPU/ユーティリティ)の異常終了を記録する(#148)。
 */
export function setupChildProcessErrorHandlers(): void {
  app.on("child-process-gone", (_event, details) => {
    logger.error(
      "main",
      `Child process gone: type=${details.type} reason=${details.reason} exitCode=${details.exitCode}`,
    );
  });
}

/**
 * レンダラー側の未処理例外/rejectionを、preload経由のIPCで受け取って記録する(#148)。
 *
 * `console-message`でも多くは拾えるが、コンソール出力を伴わない失敗
 * (`window.onerror`のみ等)を取りこぼさないための明示的な経路。
 */
export function logFromRenderer(level: unknown, source: string, message: string): void {
  // レンダラーから渡された値は信頼しない(#148レビュー指摘: 未検証のキーで
  // `logger[...]`を引くとundefined呼び出しになり、ログ自体が落ちる)。
  const safeLevel = normalizeLogLevel(level);
  logger[safeLevel.toLowerCase() as Lowercase<LogLevel>](`renderer:${source}`, message);
}

/**
 * プロセス全体の未処理例外や rejection を捕捉してログに記録する。
 */
export function setupGlobalErrorHandlers(): void {
  process.on("uncaughtException", (err) => {
    logger.error("main", "Uncaught exception:", err);
  });

  process.on("unhandledRejection", (reason) => {
    logger.error("main", "Unhandled rejection:", reason);
  });
}
