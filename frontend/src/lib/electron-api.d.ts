/**
 * preload(#78)が `contextBridge.exposeInMainWorld("api", ...)` で公開する API の型。
 * ブラウザ単独起動時は `window.api` が存在しないため、利用側は必ず optional chaining で扱う。
 */

export interface BackendInfo {
  baseUrl: string;
  token: string;
}

export type BackendStatus = "starting" | "ready" | "error";

export interface PickedFile {
  name: string;
  data: Uint8Array;
}

export interface AmeElectronApi {
  getBackendInfo(): Promise<BackendInfo>;
  onBackendStatus(cb: (status: BackendStatus, detail?: string) => void): () => void;
  /**
   * ネイティブファイルダイアログで音声ファイルを選択する。sandbox:true の renderer は
   * fs にアクセスできないため、選択されたファイルの中身は main プロセスが読み取って
   * ここで返す(楽譜/ジョブデータをIPCに乗せない、という制約とは無関係の入力経路)。
   */
  openFileDialog(): Promise<PickedFile[] | null>;
  saveFileDialog(options: {
    defaultPath?: string;
    filters?: { name: string; extensions: string[] }[];
  }): Promise<string | null>;
  showItemInFolder(path: string): void;
  openExternal(url: string): void;
  maximizeWindow(): void;
  unmaximizeWindow(): void;
  isWindowMaximized(): Promise<boolean>;
}

declare global {
  interface Window {
    api?: AmeElectronApi;
  }
}
