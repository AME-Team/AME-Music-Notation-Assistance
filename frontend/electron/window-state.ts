import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { app, type BrowserWindow, screen } from "electron";

/** ウィンドウ位置・サイズの永続化(#15)。専用ライブラリを増やさず自前のJSONで実装する。 */

interface WindowState {
  width: number;
  height: number;
  x?: number;
  y?: number;
  isMaximized?: boolean;
}

const DEFAULT_STATE: WindowState = { width: 1280, height: 800 };

function statePath(): string {
  return path.join(app.getPath("userData"), "window-state.json");
}

export function loadWindowState(): WindowState {
  try {
    const raw = readFileSync(statePath(), "utf-8");
    const state = JSON.parse(raw) as WindowState;
    const fitsOnScreen = screen
      .getAllDisplays()
      .some(
        (d) =>
          state.x !== undefined &&
          state.y !== undefined &&
          state.x >= d.bounds.x &&
          state.y >= d.bounds.y &&
          state.x < d.bounds.x + d.bounds.width &&
          state.y < d.bounds.y + d.bounds.height,
      );
    return fitsOnScreen ? state : { ...state, x: undefined, y: undefined };
  } catch {
    return DEFAULT_STATE;
  }
}

export function trackWindowState(win: BrowserWindow): void {
  const save = () => {
    const bounds = win.getBounds();
    const state: WindowState = { ...bounds, isMaximized: win.isMaximized() };
    try {
      writeFileSync(statePath(), JSON.stringify(state), "utf-8");
    } catch {
      // 保存に失敗しても致命的ではない
    }
  };
  win.on("resize", save);
  win.on("move", save);
  win.on("close", save);
}
