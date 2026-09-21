import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** ログフォルダ内の最新ファイルの中身を返す(まだ無ければ空文字)。 */
function readNewestLog(logsDir: string): string {
  const files = readdirSync(logsDir).filter((file) => file.endsWith(".log"));
  if (files.length === 0) return "";
  const newest = files.sort().at(-1) ?? "";
  return readFileSync(path.join(logsDir, newest), "utf-8");
}

/**
 * #148: 「フロント〜バックのエラー/Warningをログに残す」ことの実地検証。
 *
 * - レンダラー側の**未処理例外**が `preload → IPC(renderer:log) → main → ファイル` の
 *   経路でログに書かれること(コンソール出力を伴わない失敗でも残る)。
 * - バックエンドの出力が**内容に応じた重大度**で書かれること(uvicorn は INFO を
 *   stderr に出すため、以前は一律`[WARN] [backend]`になっていた)。
 */
test("renderer errors and backend output are written to the log file", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  // ログの検証を決定的にするため、userDataを一時ディレクトリへ隔離する
  // (共有userDataだと同日の過去ログが混ざり、否定条件が誤って落ちる)。
  const userDataDir = mkdtempSync(path.join(tmpdir(), "ame-e2e-logs-"));
  const app = await electron.launch({ args: [mainPath, `--user-data-dir=${userDataDir}`] });
  const page = await app.firstWindow();

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    // mainプロセス側ではテストのimport(`path`)は使えないため、パス文字列だけ受け取り、
    // 結合はテスト側で行う。
    const userDataPath = await app.evaluate(({ app: electronApp }) =>
      electronApp.getPath("userData"),
    );
    const logsDir = path.join(userDataPath, "logs");
    // 隔離したuserDataを実際に使っていることを確認する(前提の検証)。
    expect(logsDir.startsWith(userDataDir)).toBe(true);

    // (1) 実際の未処理例外。
    await page.evaluate(() => {
      setTimeout(() => {
        throw new Error("e2e-log-probe-renderer-error");
      }, 0);
    });

    await expect
      .poll(() => readNewestLog(logsDir), { timeout: 15_000 })
      .toContain("e2e-log-probe-renderer-error");

    // (2) 未処理rejection(これも`console-message`経由でcaptured される)。
    // 実測(#149レビュー指摘への回答): Chromiumは未処理例外と未処理rejectionの
    // **両方をconsoleへ出す**ため、`console-message`経路で十分に拾える。
    // 一度preload側で`window.onerror`を捕まえてIPCで転送する実装を入れたが、
    // `contextIsolation: true`では**preloadの(isolated worldの)リスナーがmain worldの
    // エラーを受け取らない**ことをe2eで計測して確認したため撤去した
    // (合成ErrorEventがログに残らない=経路が不発)。
    await page.evaluate(() => {
      void Promise.reject(new Error("e2e-log-probe-rejection"));
    });

    await expect
      .poll(() => readNewestLog(logsDir), { timeout: 15_000 })
      .toContain("e2e-log-probe-rejection");

    const logText = readNewestLog(logsDir);
    // バックエンド(uvicorn)のINFOがINFOとして記録されていること。
    expect(logText).toContain("[INFO] [backend]");
    // 以前はストリーム単位で一律WARNにしていたため、INFO行がWARNとして出ていた。
    expect(logText).not.toContain("[WARN] [backend] INFO:");
  } finally {
    await app.close();
  }
});
