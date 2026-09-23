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
  /** #146: 現在のバックエンド状態。購読前に送られた遷移を取り戻すために使う。 */
  getBackendStatus(): Promise<{ status: BackendStatus; detail?: string }>;
  /**
   * #158: ネイティブメニュー「編集 > 設定」が選ばれたときに呼ばれる。
   * 戻り値は購読解除の関数。**旧preloadには存在しない**ため optional にしてある
   * (`getBackendStatus`と同じく、rendererとpreloadは別々にビルドされる)。
   */
  onOpenSettings?(cb: () => void): () => void;
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

/**
 * UI刷新(M0動作確認用の可視ボタンを撤去): e2e(`e2e/app.spec.ts`)がSSE経由の
 * ジョブ進捗を検証するための非表示フック。通常のUI操作導線には出さない。
 */
export interface AmeTestHooks {
  runDummyJob(projectId: string): Promise<string>;
}

declare global {
  interface Window {
    api?: AmeElectronApi;
    __ameTestHooks?: AmeTestHooks;
  }
}
