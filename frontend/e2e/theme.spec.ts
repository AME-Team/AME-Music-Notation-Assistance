import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #152: 設定のテーマ切替(ダーク/ライト、既定=ダーク)の実地検証。
 *
 * - 起動直後は**ダーク**(`<html class="dark">`)。設定で切り替えると即座に反映される。
 * - 選択はlocalStorageへ保存され、リロード後も維持される。
 */
test("theme defaults to dark, toggles in settings, and persists", async () => {
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

    // 既定はダーク。
    await expect.poll(() => page.evaluate(() => document.documentElement.className)).toBe("dark");
    expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("dark");

    // 設定の「ライト」を選ぶと即座にライトへ。
    await page.getByRole("radio", { name: "ライト" }).check();
    await expect.poll(() => page.evaluate(() => document.documentElement.className)).toBe("");
    expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");
    expect(await page.evaluate(() => window.localStorage.getItem("ame.theme"))).toBe("light");

    // リロードしても維持される(保存されている)。
    await page.reload();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.dataset.theme), {
        timeout: 15_000,
      })
      .toBe("light");
    await expect(page.getByRole("radio", { name: "ライト" })).toBeChecked();

    // ダークへ戻せる。
    await page.getByRole("radio", { name: "ダーク" }).check();
    await expect.poll(() => page.evaluate(() => document.documentElement.className)).toBe("dark");
  } finally {
    await app.close();
  }
});
