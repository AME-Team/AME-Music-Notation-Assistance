import { existsSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #65 FR-18: プロジェクトの単一アーカイブ書き出し/読み込みを実アプリで検証する。
 *
 * 「エクスポート」ボタン(`<a download>`方式、`exportScore`と同じ経路)→
 * ダウンロード → 「アーカイブを読み込む」でファイル選択、という実際の
 * ユーザー操作をそのままなぞる(バックエンドのモックは行わない。#160の教訓:
 * このアプリはCSP注入のため`page.route()`によるAPIモックが機能しない
 * 既知のPlaywright/Electronバグがある)。
 *
 * `page.on('download')`は使わない: ElectronアプリではPlaywrightのdownload
 * イベントが信頼できない(upstream既知の制限、実測でも30秒タイムアウトした:
 * microsoft/playwright#20445)。代わりに`AME_E2E_DOWNLOADS_DIR`でダウンロード
 * 先を隔離した一時ディレクトリへ差し替え(`electron/main.ts`参照)、そこへ
 * ファイルが実際に現れるまでポーリングする。
 */

function makeTinyWavFile(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "archive-e2e-source.wav");
  const dataSize = 800 * 2;
  const buffer = Buffer.alloc(44 + dataSize);
  buffer.write("RIFF", 0);
  buffer.writeUInt32LE(36 + dataSize, 4);
  buffer.write("WAVE", 8);
  buffer.write("fmt ", 12);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(8000, 24);
  buffer.writeUInt32LE(16000, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36);
  buffer.writeUInt32LE(dataSize, 40);
  writeFileSync(filePath, buffer);
  return filePath;
}

test("project archive export/import round-trips through the real UI (#65)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const downloadsDir = mkdtempSync(path.join(tmpdir(), "ame-e2e-downloads-"));
  const app = await electron.launch({
    args: [mainPath],
    env: { ...process.env, AME_E2E_DOWNLOADS_DIR: downloadsDir },
  });
  const page = await app.firstWindow();

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    await page.getByLabel("音声ファイルを選択").setInputFiles(makeTinyWavFile());
    await expect(page.getByText("archive-e2e-source.wav")).toBeVisible({ timeout: 10_000 });
    // アップロード直後は1件だけ表示されている。
    await expect(page.getByText("archive-e2e-source.wav")).toHaveCount(1);

    // 他のe2eスペックと同じワークスペースを共有するため(#157)、既に他の
    // プロジェクトの行が存在しうる。「エクスポート」ボタンをページ全体から
    // 探すと複数行にマッチしてstrict mode違反になるため、このプロジェクトの
    // 行(`<li>`)に絞り込む。
    const row = page.getByRole("listitem").filter({ hasText: "archive-e2e-source.wav" });
    await row.getByRole("button", { name: "エクスポート" }).click();
    const archivePath = path.join(downloadsDir, "archive.ameproj");
    await expect.poll(() => existsSync(archivePath), { timeout: 15_000 }).toBe(true);

    await page.getByLabel("アーカイブファイルを選択").setInputFiles(archivePath);

    // 読み込み後、同名("archive-e2e-source.wav")のプロジェクトが2件になる(元 + 読み込んだ複製)。
    await expect(page.getByText("archive-e2e-source.wav")).toHaveCount(2, { timeout: 10_000 });
  } finally {
    await app.close();
  }

  // ダウンロードが本当にElectronの`will-download`経由で行われたこと自体の
  // 傍証として、隔離ディレクトリにファイルが残っていることを確認する。
  expect(readdirSync(downloadsDir)).toContain("archive.ameproj");
});
