import path from "node:path";
import { app, BrowserWindow, dialog, ipcMain, Menu, shell } from "electron";
import { type BackendHandle, startBackend } from "./backend";
import { buildMenu } from "./menu";
import { loadWindowState, trackWindowState } from "./window-state";

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

  // NFR-17: リモートコンテンツを一切読み込まない(全てローカルバンドル)。
  win.webContents.session.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [
          "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
            "img-src 'self' data: blob:; media-src 'self' blob: http://127.0.0.1:*; " +
            // #27: `<a download href="blob:...">.click()`(エクスポートのファイル
            // 保存)はChromiumではconnect-srcの対象になる(img-src/media-srcの
            // blob:許可だけでは足りない、実機検証で判明)。
            "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:* blob:",
        ],
      },
    });
  });

  // 外部URLはアプリ内で開かず既定ブラウザに渡す。
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  if (state.isMaximized) win.maximize();
  trackWindowState(win);

  win.once("ready-to-show", () => win.show());

  if (DEV_SERVER_URL) {
    void win.loadURL(DEV_SERVER_URL);
  } else {
    void win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

  return win;
}

function registerIpcHandlers(): void {
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
}

async function stopBackendAndQuit(): Promise<void> {
  if (backend) {
    await backend.stop();
    backend = null;
  }
}

app.on("second-instance", () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

app.whenReady().then(async () => {
  Menu.setApplicationMenu(buildMenu());
  registerIpcHandlers();
  mainWindow = createWindow();

  backend = await startBackend((status, detail) => {
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
