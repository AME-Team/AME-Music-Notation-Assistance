import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #1/#15 の受け入れ条件を検証するスモークテスト:
 * Electron アプリとして起動し、MP3(相当)をアップロードすると、
 * ダミージョブの進捗が SSE で UI に流れ、アプリ終了後に Python バックエンドが残らない。
 *
 * 実行には `npm run build` 済みであること、かつ backend/.venv がセットアップ済み
 * (uv sync 済み)であることが前提。
 */

function makeTinyWavFile(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "smoke-test.wav");

  const numSamples = 800;
  const dataSize = numSamples * 2;
  const buffer = Buffer.alloc(44 + dataSize);
  buffer.write("RIFF", 0);
  buffer.writeUInt32LE(36 + dataSize, 4);
  buffer.write("WAVE", 8);
  buffer.write("fmt ", 12);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20); // PCM
  buffer.writeUInt16LE(1, 22); // mono
  buffer.writeUInt32LE(8000, 24); // sample rate
  buffer.writeUInt32LE(16000, 28); // byte rate
  buffer.writeUInt16LE(2, 32); // block align
  buffer.writeUInt16LE(16, 34); // bits per sample
  buffer.write("data", 36);
  buffer.writeUInt32LE(dataSize, 40);
  writeFileSync(filePath, buffer);
  return filePath;
}

/** `window.api.getBackendInfo()` が返すバックエンド接続情報(#15)。 */
type BackendInfo = { baseUrl: string; token: string };

test("Electron app boots, runs a dummy job over SSE, and cleans up the backend on quit", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({ args: [mainPath] });
  const page = await app.firstWindow();

  let backendInfo: BackendInfo | undefined;
  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    const wavPath = makeTinyWavFile();
    await page.locator('input[type="file"]').setInputFiles(wavPath);
    await expect(page.getByText("smoke-test.wav")).toBeVisible({ timeout: 10_000 });

    await page.getByText("smoke-test.wav").click();
    await page.getByRole("button", { name: /ダミージョブを実行/ }).click();

    await expect(page.getByText("完了")).toBeVisible({ timeout: 15_000 });

    backendInfo = await page.evaluate(() => window.api?.getBackendInfo());
    expect(backendInfo).toBeTruthy();

    // #146: 現在値取得(購読前に送られた状態遷移の取り戻し)が実アプリで機能すること。
    // これを欠くと、レンダラーの起動が遅い場合にUIが「バックエンドを起動しています」
    // のまま固まる(実際にWindows実機の開発モードで発生した)。
    await expect
      .poll(async () => (await page.evaluate(() => window.api?.getBackendStatus()))?.status, {
        timeout: 15_000,
      })
      .toBe("ready");
  } finally {
    // #145レビュー指摘: 途中のassertで失敗してもElectronを確実に終了させる。
    await app.close();
  }

  // アプリ終了後、Python バックエンドがポートに応答しないことを確認する
  // (プロセスが確実に終了していることの間接的な証拠)。
  await expect(async () => {
    await expect(fetch(`${backendInfo?.baseUrl}/health`)).rejects.toThrow();
  }).toPass({ timeout: 5_000 });
});
