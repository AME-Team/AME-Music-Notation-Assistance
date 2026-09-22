import { Menu, type MenuItemConstructorOptions, shell } from "electron";
import { getLogsDir } from "./logger";

/** #158: メニュー項目が実行する操作(mainプロセス側のコールバック)。 */
/** レンダラーが購読を確立したかを判定するためのチャンネル(preloadが送る)。 */
export const RENDERER_SETTINGS_READY_CHANNEL = "menu:settings-ready";

export interface MenuActions {
  /**
   * 「編集 > 設定」が選ばれたときに呼ぶ。
   *
   * メニューは`mainWindow`の生成前に組まれるため、ここでは「どう通知するか」だけを
   * 受け取り、windowの存在確認は呼び出し側(`main.ts`)が行う。
   */
  openSettings: () => void;
  /**
   * 「設定」項目を有効にするか。#146 と同種の問題(購読前に通知を送ると失われる)を
   * 避けるため、rendererが購読を確立するまでは**無効**にして「押しても何も起きない」
   * 状態を作らない。有効化のタイミングでメニューを組み直す。
   */
  openSettingsEnabled: boolean;
}

/** ネイティブメニュー(#15)。M0 では最小限(標準ロールのみ)。 */
export function buildMenu(actions: MenuActions): Menu {
  const template: MenuItemConstructorOptions[] = [
    {
      label: "ファイル",
      submenu: [{ role: "quit", label: "終了" }],
    },
    {
      label: "編集",
      submenu: [
        { role: "undo", label: "元に戻す" },
        { role: "redo", label: "やり直す" },
        { type: "separator" },
        { role: "cut", label: "切り取り" },
        { role: "copy", label: "コピー" },
        { role: "paste", label: "貼り付け" },
        { type: "separator" },
        // #158: 設定モーダルを開く(画面デザイン上、設定はメインウィンドウに常設せず
        // メニューから開くダイアログに置く)。acceleratorは一般的な
        // 「環境設定」のショートカットに合わせる。
        {
          label: "設定",
          accelerator: "CmdOrCtrl+,",
          enabled: actions.openSettingsEnabled,
          click: () => actions.openSettings(),
        },
      ],
    },
    {
      label: "表示",
      submenu: [
        { role: "reload", label: "再読み込み" },
        { role: "toggleDevTools", label: "開発者ツール" },
        { type: "separator" },
        { role: "resetZoom", label: "実際のサイズ" },
        { role: "zoomIn", label: "拡大" },
        { role: "zoomOut", label: "縮小" },
        { type: "separator" },
        { role: "togglefullscreen", label: "フルスクリーン切替" },
      ],
    },
    {
      label: "ヘルプ",
      submenu: [
        {
          label: "ログフォルダを開く",
          click: () => {
            void shell.openPath(getLogsDir());
          },
        },
        { type: "separator" },
        {
          label: "リポジトリを開く",
          click: () =>
            shell.openExternal("https://github.com/AME-Team/AME-Music-Notation-Assistance"),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}
