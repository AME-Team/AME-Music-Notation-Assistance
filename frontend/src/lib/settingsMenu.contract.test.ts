/**
 * #158レビュー観点: 「編集 > 設定」の通知は main プロセスの `webContents.send` と
 * preload の `ipcRenderer.on` が**同じチャンネル名文字列**を使わないと成立しないが、
 * 名前が違っても例外は出ず「メニューを押しても何も起きない」という形で黙って壊れる
 * (型でもビルドでも検出できないクロスファイル契約)。
 *
 * そこで両者のソースを読んで、レンダラー側の定数と一致することを検証する
 * (`noteFlags.contract.test.ts` と同じ方針)。定義が見つからない場合は
 * 「変更が無かった」と誤判定しないよう明示的に失敗させる。
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { OPEN_SETTINGS_CHANNEL } from "./settingsMenu";

const PRELOAD_TS = new URL("../../electron/preload.ts", import.meta.url);
const MAIN_TS = new URL("../../electron/main.ts", import.meta.url);

/** レンダラー準備完了の通知チャンネル(#158)。preloadが送り、mainが受ける。 */
const SETTINGS_READY_CHANNEL = "menu:settings-ready";

describe("settings menu channel contract (#158)", () => {
  it("preloadは同じチャンネル名を購読している", () => {
    const source = readFileSync(PRELOAD_TS, "utf-8");
    expect(source).toContain(`ipcRenderer.on("${OPEN_SETTINGS_CHANNEL}", listener)`);
  });

  it("mainプロセスは同じチャンネル名を送っている", () => {
    const source = readFileSync(MAIN_TS, "utf-8");
    expect(source).toContain(`webContents.send("${OPEN_SETTINGS_CHANNEL}")`);
  });

  it("準備完了の通知はpreloadが送り、mainが受けている", () => {
    // 購読前に送られた通知は失われるため、mainはこの通知までメニュー項目を無効に
    // している。チャンネル名がずれると「設定が永久に無効」という形で壊れる。
    expect(readFileSync(PRELOAD_TS, "utf-8")).toContain(
      `ipcRenderer.send("${SETTINGS_READY_CHANNEL}")`,
    );
    expect(readFileSync(MAIN_TS, "utf-8")).toContain(`ipcMain.on(RENDERER_SETTINGS_READY_CHANNEL`);
  });
});
