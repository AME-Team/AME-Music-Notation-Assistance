import { appendFile, mkdir, readdir, stat, unlink } from "node:fs/promises";
import path from "node:path";
import { app, type WebContents } from "electron";

/**
 * ログ管理モジュール。
 * - エラーログ、デバッグログ、情報ログを日付別ファイル (app-YYYY-MM-DD.log) に記録。
 * - ログ保存期間は 1 週間 (7 日間)。古いログファイルは自動削除して肥大化を防止。
 * - メインプロセス、レンダラープロセス、バックエンドプロセスの全出力を集約。
 */

export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

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
  webContents.on("console-message", (_event, level, message, line, sourceId) => {
    const loc = sourceId ? ` (${sourceId}:${line})` : "";
    const msg = `${message}${loc}`;
    switch (level) {
      case 0: // Verbose / Debug
        logger.debug("renderer", msg);
        break;
      case 1: // Info
        logger.info("renderer", msg);
        break;
      case 2: // Warning
        logger.warn("renderer", msg);
        break;
      case 3: // Error
        logger.error("renderer", msg);
        break;
      default:
        logger.info("renderer", msg);
    }
  });

  webContents.on("render-process-gone", (_event, details) => {
    logger.error(
      "renderer",
      `Render process gone: reason=${details.reason}, exitCode=${details.exitCode}`,
    );
  });
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
