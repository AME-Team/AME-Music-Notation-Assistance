import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #167: 「②テンポ・拍の検出」の画面で**検出結果そのもの**(テンポ・拍子・拍の数)が
 * 確認できることの回帰テスト。
 *
 * 以前はこの画面に波形(ビート線入り)と補正フォームしか無く、BPMや拍がどこにあるのかを
 * 画面から読み取れなかった。ここでは実アプリを起動し、②の画面に検出結果パネル
 * (テンポ/拍子/拍の数/平均拍間隔)と拍一覧が出ること、および一覧の全件表示が
 * 機能することを検証する。
 *
 * ## 実装上の注意
 *
 * - `page.route()`によるAPIモックは、ElectronのCSPヘッダ注入
 *   (`session.webRequest.onHeadersReceived`)と併用すると`status: 0`に壊れる
 *   Playwright側の既知バグがある(#160で実測)。`AME_E2E_DISABLE_CSP_HEADER`で
 *   このテストに限り注入を止める(CSP自体の回帰は`e2e/csp.spec.ts`が検証する)。
 * - ②は①音源分離が完了扱いでないと選択できない(ステップのロック)ため、
 *   単一プロジェクト取得の応答だけを差し替えて①〜④を完了済みに見せかける。
 *   実際のDSP推論(分離・ビート推定)は走らせない。
 * - ビート推定結果(`GET .../analysis/beatmap`)は、決定的な検証のため
 *   4/4・120 BPM・20拍/5小節のフィクスチャで差し替える。
 */

/** 検証に使う拍の数(MAX_VISIBLE_ROWSを超えさせ、全件表示の切替も確認する)。 */
const BEAT_COUNT = 20;
const BEATS_PER_BAR = 4;
const BEAT_INTERVAL_SEC = 0.5;

/** 4/4・120 BPM相当(各拍0.5秒)の決定的なbeatmapを作る。 */
function buildBeatmap() {
  const beats = [];
  const downbeats: number[] = [];
  for (let index = 0; index < BEAT_COUNT; index += 1) {
    const bar = Math.floor(index / BEATS_PER_BAR) + 1;
    const beatInBar = (index % BEATS_PER_BAR) + 1;
    const timeSec = Number((index * BEAT_INTERVAL_SEC).toFixed(2));
    beats.push({ time_sec: timeSec, beat_in_bar: beatInBar, bar });
    if (beatInBar === 1) downbeats.push(timeSec);
  }

  return {
    beats,
    downbeats_sec: downbeats,
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    // `tempo_map`は先頭の拍を含まない(backend/app/pipeline/beat.pyの`_tempo_map`)。
    tempo_map: beats.slice(1).map((beat) => ({
      bar: beat.bar,
      beat: beat.beat_in_bar,
      bpm: 120,
    })),
    confidence: 0.9928,
    source: "auto",
  };
}

/** app.spec.ts と同じ最小WAV(プロジェクトを作るためだけに使う)。 */
function makeTinyWavFile(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "beat-step.wav");
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

test("beat step shows the detected tempo, time signature and beats (#167)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({
    args: [mainPath],
    env: { ...process.env, AME_E2E_DISABLE_CSP_HEADER: "1" },
  });
  const page = await app.firstWindow();

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    // ビート推定の成果物を差し替える(推論は走らせない)。
    await page.route("**/analysis/beatmap", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(buildBeatmap()),
      }),
    );
    // ①〜④を完了済みに見せかけ、②を選択可能にする(ステップのロック解除)。
    await page.route(/\/api\/projects\/[^/]+$/, async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      const doneStage = { status: "succeeded", progress: 1, stale: false };
      body.stages = {
        separate: doneStage,
        beat: doneStage,
        transcribe: doneStage,
        quantize: doneStage,
      };
      return route.fulfill({ response, json: body });
    });

    await test.step("プロジェクトを作成して開く", async () => {
      await page.getByLabel("音声ファイルを選択").setInputFiles(makeTinyWavFile());
      await expect(page.getByText("beat-step.wav")).toBeVisible({ timeout: 10_000 });
      await page.getByText("beat-step.wav").click();
    });

    await test.step("②テンポ・拍の検出を開く", async () => {
      // サイドバーから明示的に移動する(自動誘導に依存しない)。
      await page.getByRole("button", { name: "2. テンポ・拍の検出" }).click();
      await expect(page.getByRole("heading", { name: "2. テンポ・拍の検出" })).toBeVisible();
    });

    const results = page.locator("section", { hasText: "検出結果" });
    // 見出し・値・拍一覧が同居するため、集計値は定義リスト(`dl`)に絞って検証する
    // (「0.50 秒」は拍一覧の時刻にも現れ、ページ全体では2要素にマッチする)。
    const summary = results.locator("dl");

    await test.step("検出結果(テンポ・拍子・拍の規模)が表示される", async () => {
      await expect(summary.getByText("120.0 BPM")).toBeVisible({ timeout: 20_000 });
      await expect(summary.getByText("ほぼ一定")).toBeVisible();
      await expect(summary.getByText("4/4")).toBeVisible();
      await expect(summary.getByText("20 拍", { exact: true })).toBeVisible();
      await expect(summary.getByText("小節 5・ダウンビート 5 箇所")).toBeVisible();
      // (9.50 - 0) / (20 - 1) = 0.50 秒
      await expect(summary.getByText("0.50 秒")).toBeVisible();
      await expect(results.getByText("信頼度 99%")).toBeVisible();
    });

    await test.step("拍一覧の全件表示を切り替えられる", async () => {
      // 初期状態は先頭12拍(1.1〜3.4)まで。13拍目(4.1)と最後(5.4)は出ない。
      await expect(results.getByText("1.1")).toBeVisible();
      await expect(results.getByText("3.4")).toBeVisible();
      await expect(results.getByText("4.1")).toHaveCount(0);
      await expect(results.getByText("5.4")).toHaveCount(0);

      await results.getByRole("button", { name: "すべて表示(20 拍)" }).click();
      await expect(results.getByText("4.1")).toBeVisible();
      await expect(results.getByText("5.4")).toBeVisible();

      // 元に戻せる。
      await results.getByRole("button", { name: "先頭 12 拍のみ表示" }).click();
      await expect(results.getByText("5.4")).toHaveCount(0);
    });
  } finally {
    await app.close();
  }
});
