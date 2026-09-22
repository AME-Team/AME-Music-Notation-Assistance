import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  type ElectronApplication,
  _electron as electron,
  expect,
  type Page,
  test,
} from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #152/#158: 設定のテーマ切替(ダーク/ライト、既定=ダーク)の実地検証。
 *
 * - 設定はメインウィンドウに常設せず、**ネイティブメニュー「編集 > 設定」**から
 *   モーダルを開く(#158)。開くまでは表示されていないこと。
 * - 起動直後は**ダーク**(`<html class="dark">`)。設定で切り替えると即座に反映される。
 * - 選択はlocalStorageへ保存され、リロード後も維持される。
 * - Escapeキーでも閉じられる。
 */

/** ネイティブメニューの「編集 > 設定」を実行する(mainプロセス側で操作)。 */
async function clickSettingsMenuItem(app: ElectronApplication): Promise<void> {
  await app.evaluate(({ Menu }) => {
    const edit = Menu.getApplicationMenu()?.items.find((item) => item.label === "編集");
    const settings = edit?.submenu?.items.find((item) => item.label === "設定");
    if (!settings) throw new Error("ネイティブメニューに 編集 > 設定 が見つかりません");
    settings.click();
  });
}

/**
 * Tabキーでフォーカスがダイアログ内に留まることを確認する(#158レビュー指摘)。
 *
 * チェック済みラジオの位置でタブ順が変わるため、**ダーク/ライト両方の状態**で
 * 実行する(既定ダークのときだけトラップが破綻していた)。
 */
async function expectFocusStaysInDialog(page: Page): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await page.keyboard.press("Tab");
    // `document.activeElement?.closest(...) !== null` は activeElement が null のとき
    // `undefined !== null` で true になり偽陽性になるため、真偽値へ畳む。
    const insideDialog = await page.evaluate(() =>
      Boolean(document.activeElement?.closest('[role="dialog"]')),
    );
    expect(insideDialog).toBe(true);
  }
}

test("theme defaults to dark, toggles in settings modal opened from the menu, and persists", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const userDataDir = mkdtempSync(path.join(tmpdir(), "ame-e2e-theme-"));
  const app = await electron.launch({
    args: [mainPath, `--user-data-dir=${userDataDir}`],
  });
  const page = await app.firstWindow();

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({
      timeout: 30_000,
    });

    // #158: 設定はメインウィンドウに常設されていない(メニューから開くまで出ない)。
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // 既定はダーク。
    await expect.poll(() => page.evaluate(() => document.documentElement.className)).toBe("dark");
    expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("dark");

    // ネイティブメニュー「編集 > 設定」でモーダルが開く。
    await clickSettingsMenuItem(app);
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByRole("dialog").getByText("画面テーマ")).toBeVisible();

    // フォーカストラップ(既定ダーク=チェック済みが先頭の状態)。
    await expectFocusStaysInDialog(page);

    // 設定の「ライト」を選ぶと即座にライトへ。
    await page.getByRole("radio", { name: "ライト" }).check();
    await expect.poll(() => page.evaluate(() => document.documentElement.className)).toBe("");
    expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");
    expect(await page.evaluate(() => window.localStorage.getItem("ame.theme"))).toBe("light");

    // フォーカストラップ(チェック済みが末尾=ライトの状態)。
    await expectFocusStaysInDialog(page);

    // Escapeキーでも閉じられる。
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // リロードしても維持される(保存されている)。
    await page.reload();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.dataset.theme), {
        timeout: 15_000,
      })
      .toBe("light");
    await clickSettingsMenuItem(app);
    await expect(page.getByRole("radio", { name: "ライト" })).toBeChecked();

    // ダークへ戻せる。
    await page.getByRole("radio", { name: "ダーク" }).check();
    await expect.poll(() => page.evaluate(() => document.documentElement.className)).toBe("dark");
  } finally {
    await app.close();
  }
});
