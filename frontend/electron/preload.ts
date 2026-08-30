import { contextBridge, ipcRenderer } from "electron";

/**
 * preload / contextIsolation / IPC ブリッジ(#78, NFR-17)。
 * ここに列挙したもの以外は renderer に公開しない。楽譜データ・ジョブ・エージェントの
 * イベントは含まない(それらは HTTP / SSE で取得する)。
 */
contextBridge.exposeInMainWorld("api", {
  getBackendInfo: () => ipcRenderer.invoke("backend:get-info"),

  onBackendStatus: (cb: (status: string, detail?: string) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, status: string, detail?: string) =>
      cb(status, detail);
    ipcRenderer.on("backend:status", listener);
    return () => ipcRenderer.removeListener("backend:status", listener);
  },

  openFileDialog: () => ipcRenderer.invoke("dialog:open-file"),

  saveFileDialog: (options: {
    defaultPath?: string;
    filters?: { name: string; extensions: string[] }[];
  }) => ipcRenderer.invoke("dialog:save-file", options),

  showItemInFolder: (path: string) => ipcRenderer.send("shell:show-item-in-folder", path),

  openExternal: (url: string) => ipcRenderer.send("shell:open-external", url),

  maximizeWindow: () => ipcRenderer.send("window:maximize"),
  unmaximizeWindow: () => ipcRenderer.send("window:unmaximize"),
  isWindowMaximized: () => ipcRenderer.invoke("window:is-maximized"),
});
