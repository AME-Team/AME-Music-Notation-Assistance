import path from "node:path";
import { app, BrowserWindow, dialog, ipcMain, Menu, shell } from "electron";
import { type BackendHandle, getBackendStatus, startBackend } from "./backend";
import {
  attachFatalErrorHandlers,
  attachWebContentsLogger,
  getLogsDir,
  initLogger,
  logger,
  setupChildProcessErrorHandlers,
  setupGlobalErrorHandlers,
} from "./logger";
import { buildMenu, RENDERER_SETTINGS_READY_CHANNEL } from "./menu";
import { loadWindowState, trackWindowState } from "./window-state";

setupGlobalErrorHandlers();
// #148: レンダラー/GPU等の子プロセスが落ちた場合もログに残す。
setupChildProcessErrorHandlers();

// #77: 単一インスタンス制御(2重起動でバックエンド/ポートが衝突するのを防ぐ)。
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

let mainWindow: BrowserWindow | null = null;
let backend: BackendHandle | null = null;

const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

function createWindow(): BrowserWindow {
  const state = loadWindowState();
  const win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    show: false,
    webPreferences: {
      // NFR-17: renderer から Node.js API へ直接アクセスさせない。
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  // レンダラープロセスのコンソールログ・クラッシュログをファイルに集約
  attachWebContentsLogger(win.webContents);
  // #148: preload読み込み失敗・ページ読み込み失敗もログへ残す。
  attachFatalErrorHandlers(win.webContents);

  // NFR-17: リモートコンテンツを一切読み込まない(全てローカルバンドル)。
  // 開発時(DEV_SERVER_URL)は Vite の Fast Refresh (Preamble) インラインスクリプトおよび
  // HMR WebSocket を許可し、本番時は 'self' の厳格な設定を維持する。
  const isDev = Boolean(DEV_SERVER_URL);
  const scriptSrc = isDev ? "'self' 'unsafe-inline'" : "'self'";
  const connectSrc = isDev
    ? "'self' http://127.0.0.1:* ws://127.0.0.1:* ws://localhost:* http://localhost:* blob:"
    : "'self' http://127.0.0.1:* ws://127.0.0.1:* blob:";

  // #160: `session.webRequest.onHeadersReceived`が登録されていると、
  // Playwrightの`page.route()`によるモック応答が全て`status: 0`に壊れる
  // (upstream既知のバグ、closed・修正予定なし: microsoft/playwright#30495)。
  // `e2e/score-preview.spec.ts`のみこの環境変数でCSPヘッダ注入を無効化して
  // 回避する(upstreamのIssueコメントにある回避策と同じ)。CSP自体の回帰は
  // `e2e/csp.spec.ts`が別途(この分岐を通さずに)検証している。
  // `!app.isPackaged`も併せて要求する(precommitレビュー指摘、MIDDLE):
  // 環境変数だけの条件だと、パッケージ済みの配布物でもこの変数が
  // 設定されていればCSPが丸ごと無効化されてしまう。開発/テスト実行
  // (`electron .`やPlaywrightからの起動)は`isPackaged`が常に`false`なので、
  // 配布物では併用してもこのバイパスは機能しない。
  const skipCspHeaderForE2e = !app.isPackaged && process.env.AME_E2E_DISABLE_CSP_HEADER === "1";
  if (!skipCspHeaderForE2e) {
    win.webContents.session.webRequest.onHeadersReceived((details, callback) => {
      callback({
        responseHeaders: {
          ...details.responseHeaders,
          "Content-Security-Policy": [
            `default-src 'self'; script-src ${scriptSrc}; style-src 'self' 'unsafe-inline'; ` +
              // #60(Windows実機検証)で発覚した実バグ: Tone.js(再生機能 #34)は
              // `new Worker(URL.createObjectURL(new Blob([...])))` で自前のクロック用
              // Workerを作るが、`worker-src`が無いと`script-src`(blob:を含まない)へ
              // フォールバックして**ブロック**される。しかも`new Worker()`は例外を
              // 投げず「生成だけ成功してonerrorで無言で死ぬ」ため、Tone側のtimeout
              // フォールバックも働かず、**contextの"tick"イベントが一度も発火しない**
              // (＝Transportの予定イベントが鳴らない)状態になっていた。実ブラウザでの
              // 計測: worker-src無しはticks=0、`worker-src 'self' blob:`でticks=24/1.2s。
              // 回帰は`frontend/e2e/csp.spec.ts`のblob Workerテストで検出する。
              "worker-src 'self' blob:; " +
              "img-src 'self' data: blob:; media-src 'self' blob: http://127.0.0.1:*; " +
              // #27: `<a download href="blob:...">.click()`(エクスポートのファイル
              // 保存)はChromiumではconnect-srcの対象になる(img-src/media-srcの
              // blob:許可だけでは足りない、実機検証で判明)。
              `connect-src ${connectSrc}`,
          ],
        },
      });
    });
  }

  // 外部URLはアプリ内で開かず既定ブラウザに渡す。
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  if (state.isMaximized) win.maximize();
  trackWindowState(win);

  win.once("ready-to-show", () => win.show());

  if (DEV_SERVER_URL) {
    logger.info("main", `Loading dev server URL: ${DEV_SERVER_URL}`);
    void win.loadURL(DEV_SERVER_URL);
  } else {
    logger.info("main", "Loading production bundle index.html");
    void win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

  return win;
}

/** rendererが購読を確立したか(#158)。メニュー項目の有効/無効に使う。 */
let settingsMenuReady = false;

/**
 * アプリケーションメニューを組み直す(#158)。
 *
 * メニューは`mainWindow`の生成前に組まれるため、通知方法(コールバック)だけを渡す。
 * rendererの購読が確立するまでは「設定」項目を無効にして「押しても何も起きない」
 * 状態を避け、準備完了の通知を受けてから組み直す。
 */
function installMenu(): void {
  Menu.setApplicationMenu(
    buildMenu({
      openSettings: () => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("menu:open-settings");
        }
      },
      openSettingsEnabled: settingsMenuReady,
    }),
  );
}

function registerIpcHandlers(): void {
  // #158: rendererが「設定」の購読を張ったら有効化する(それ以前の通知は届かない)。
  ipcMain.on(RENDERER_SETTINGS_READY_CHANNEL, () => {
    if (settingsMenuReady) return;
    settingsMenuReady = true;
    installMenu();
  });

  // #146: 現在のバックエンド状態を返す(購読前に送られたイベントの取り戻し用)。
  ipcMain.handle("backend:get-status", () => getBackendStatus());

  ipcMain.handle("backend:get-info", () => {
    if (!backend) throw new Error("backend is not ready");
    return { baseUrl: backend.baseUrl, token: backend.token };
  });

  ipcMain.handle("dialog:open-file", async () => {
    if (!mainWindow) return null;
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openFile"],
      filters: [{ name: "音声ファイル", extensions: ["mp3", "wav", "flac", "m4a"] }],
    });
    if (result.canceled || result.filePaths.length === 0) return null;
    const { readFile } = await import("node:fs/promises");
    return Promise.all(
      result.filePaths.map(async (filePath) => ({
        name: path.basename(filePath),
        data: await readFile(filePath),
      })),
    );
  });

  ipcMain.handle(
    "dialog:save-file",
    async (
      _event,
      options: { defaultPath?: string; filters?: { name: string; extensions: string[] }[] },
    ) => {
      if (!mainWindow) return null;
      const result = await dialog.showSaveDialog(mainWindow, options);
      return result.canceled ? null : (result.filePath ?? null);
    },
  );

  ipcMain.on("shell:show-item-in-folder", (_event, filePath: string) => {
    shell.showItemInFolder(filePath);
  });

  ipcMain.on("shell:open-external", (_event, url: string) => {
    void shell.openExternal(url);
  });

  ipcMain.on("window:maximize", () => mainWindow?.maximize());
  ipcMain.on("window:unmaximize", () => mainWindow?.unmaximize());
  ipcMain.handle("window:is-maximized", () => mainWindow?.isMaximized() ?? false);
  ipcMain.handle("logs:open-dir", async () => {
    await shell.openPath(getLogsDir());
  });
}

async function stopBackendAndQuit(): Promise<void> {
  if (backend) {
    logger.info("main", "Stopping Python backend...");
    await backend.stop();
    backend = null;
    logger.info("main", "Python backend stopped.");
  }
}

app.on("second-instance", () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

app.whenReady().then(async () => {
  await initLogger();
  logger.info("main", "AME Music Notation Assistance starting...");
  installMenu();
  registerIpcHandlers();
  mainWindow = createWindow();

  backend = await startBackend((status, detail) => {
    logger.info("main", `Backend status: ${status}${detail ? ` (${detail})` : ""}`);
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("backend:status", status, detail);
    }
  });
});

// NFR-18: アプリ終了・クラッシュ時に Python バックエンドを確実に終了する。
app.on("window-all-closed", () => {
  void stopBackendAndQuit().finally(() => {
    if (process.platform !== "darwin") app.quit();
  });
});

app.on("before-quit", (event) => {
  if (backend) {
    event.preventDefault();
    void stopBackendAndQuit().finally(() => app.exit(0));
  }
});
