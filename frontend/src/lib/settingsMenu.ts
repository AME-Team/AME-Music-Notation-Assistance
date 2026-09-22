/**
 * #158: ネイティブメニュー「編集 > 設定」の購読ロジック。
 *
 * mainプロセスはメニュー項目が選ばれたときに `menu:open-settings` を送る。
 * rendererとpreloadは別々にビルドされる(`src` と `dist-electron`)ため、
 * 開発中はバージョンがずれ得る。**旧preloadにはこのAPIが無い**ので、
 * `getBackendStatus`(#146)と同じく機能検出して落とさない。
 *
 * DOMに依存しない純粋なロジックにして単体テスト可能にしている。
 */

/** mainプロセスが送るチャンネル名(preloadの`ipcRenderer.on`と一致させること)。 */
export const OPEN_SETTINGS_CHANNEL = "menu:open-settings";

export interface SettingsMenuApi {
  /** 旧preloadでは未定義になり得るため optional。 */
  onOpenSettings?(cb: () => void): () => void;
}

/**
 * 「設定」メニューの選択を購読する。戻り値は購読解除の関数。
 *
 * APIが無い(旧preload、またはブラウザ単独起動)場合は何もせず、購読解除だけを返す。
 */
export function watchOpenSettings(
  api: SettingsMenuApi | undefined,
  onOpen: () => void,
): () => void {
  if (!api || typeof api.onOpenSettings !== "function") return () => undefined;
  return api.onOpenSettings(onOpen);
}
