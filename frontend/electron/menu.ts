import { Menu, type MenuItemConstructorOptions, shell } from "electron";

/** ネイティブメニュー(#15)。M0 では最小限(標準ロールのみ)。 */
export function buildMenu(): Menu {
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
          label: "リポジトリを開く",
          click: () =>
            shell.openExternal("https://github.com/AME-Team/AME-Music-Notation-Assistance"),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}
