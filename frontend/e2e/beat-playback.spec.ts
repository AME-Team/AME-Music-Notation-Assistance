import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #171: 「②テンポ・拍の検出」で、推定したビートを**耳で**確かめられること、および
 * 「ビートグリッド補正」の各操作の**現在値**がその場に出ていることの回帰テスト。
 *
 * - 「再生」で原音が再生され、再生ヘッド(位置表示)が進むこと
 * - 「ビートにクリック音」の入切ができること
 * - 各補正ブロック(全体オフセット/固定BPM/ダウンビート/拍子)に現在値が出ること
 * - 入力中の値で「適用後」の値が併記されること(数値を入れてから適用するまで、
 *   何が変わるのか分からなかった)
 *
 * ## 実装上の注意
 *
 * - `page.route()`によるAPIモックは、ElectronのCSPヘッダ注入と併用すると壊れる
 *   既知バグがあるため、`AME_E2E_DISABLE_CSP_HEADER`で注入を止める(#160で実測)。
 * - ②は①が完了扱いでないと選択できないため、単一プロジェクト取得の応答だけを
 *   差し替えて①〜④を完了済みに見せかける。DSP推論は走らせない。
 * - 音源は3分(180秒)の無音WAV。再生位置が動くことを確かめるため、ある程度の長さが要る。
 */

const DURATION_SEC = 180;
const SAMPLE_RATE = 8000;
const BEAT_COUNT = 360;
const BEAT_INTERVAL_SEC = 0.5;

/** 4/4・120 BPM相当(各拍0.5秒)の決定的なbeatmap。 */
function buildBeatmap() {
  const beats = [];
  const downbeats: number[] = [];
  for (let index = 0; index < BEAT_COUNT; index += 1) {
    const bar = Math.floor(index / 4) + 1;
    const beatInBar = (index % 4) + 1;
    const timeSec = Number((index * BEAT_INTERVAL_SEC).toFixed(2));
    beats.push({ time_sec: timeSec, beat_in_bar: beatInBar, bar });
    if (beatInBar === 1) downbeats.push(timeSec);
  }

  return {
    beats,
    downbeats_sec: downbeats,
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    tempo_map: beats.slice(1).map((beat) => ({ bar: beat.bar, beat: beat.beat_in_bar, bpm: 120 })),
    confidence: 0.99,
    source: "auto",
  };
}

/** 指定秒数の無音WAV(8kHz・モノラル・16bit)を書く。 */
function makeSilentWavFile(seconds: number): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "playback.wav");
  const dataSize = Math.round(seconds * SAMPLE_RATE) * 2;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + dataSize, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(dataSize, 40);
  writeFileSync(filePath, Buffer.concat([header, Buffer.alloc(dataSize)]));
  return filePath;
}

test("beat step plays the audio with beat clicks and shows current values (#171)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({
    args: [mainPath],
    env: { ...process.env, AME_E2E_DISABLE_CSP_HEADER: "1" },
  });
  const page = await app.firstWindow();

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    await page.route("**/analysis/beatmap", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(buildBeatmap()),
      }),
    );
    // 原音の取得は初回の「再生」まで遅延させる(②を開いただけで数十MBの音源を
    // 載せない)。その回数を数えて検証する。同じURLはグローバルの再生バー
    // (`AudioPlayer`)も取得するため、「再生を押す前後で増えること」だけを見る。
    let audioRequests = 0;
    let audioRequestsBeforePlay = 0;
    await page.route("**/audio/original", (route) => {
      audioRequests += 1;
      return route.continue();
    });
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

    await test.step("プロジェクトを作成して②テンポ・拍の検出を開く", async () => {
      await page.getByLabel("音声ファイルを選択").setInputFiles(makeSilentWavFile(DURATION_SEC));
      await expect(page.getByText("playback.wav")).toBeVisible({ timeout: 20_000 });
      await page.getByText("playback.wav").click();
      await page.getByRole("button", { name: "2. テンポ・拍の検出" }).click();
      await expect(page.getByRole("heading", { name: "2. テンポ・拍の検出" })).toBeVisible();
      // 波形(と再生ボタン)が出るまで待つ。
      await expect(page.getByTestId("beat-playhead")).toBeAttached({ timeout: 30_000 });
      audioRequestsBeforePlay = audioRequests;
    });

    const viewer = page.locator("section", { hasText: "波形とビートグリッド" });
    const editor = page.locator("section", { hasText: "ビートグリッド補正" });

    await test.step("ビートグリッド補正の各項目に現在値が出ている", async () => {
      const offsetBlock = editor.locator("section", { hasText: "全体オフセット" });
      await expect(offsetBlock).toContainText("現在");
      await expect(offsetBlock).toContainText("先頭の拍");
      await expect(offsetBlock).toContainText("0.00 秒");
      await expect(offsetBlock).toContainText("最後の拍");
      await expect(offsetBlock).toContainText("179.50 秒");
      await expect(offsetBlock).toContainText("平均拍間隔");
      await expect(offsetBlock).toContainText("0.50 秒");

      const bpmBlock = editor.locator("section", { hasText: "固定BPMへ上書き" });
      await expect(bpmBlock).toContainText("120.0 BPM");
      await expect(bpmBlock).toContainText("検出したテンポの幅");

      const downbeatBlock = editor.locator("section", { hasText: "ダウンビートの位置" });
      await expect(downbeatBlock).toContainText("90 箇所");
      await expect(downbeatBlock).toContainText("360 拍");

      const meterBlock = editor.locator("section", { hasText: "小節番号と拍子" });
      await expect(meterBlock).toContainText("4/4");
      await expect(meterBlock).toContainText("1〜90");
      await expect(meterBlock).toContainText("4 拍");
    });

    await test.step("入力中の値で適用後の値が併記される", async () => {
      const offsetBlock = editor.locator("section", { hasText: "全体オフセット" });
      await offsetBlock.getByPlaceholder("例: 0.25").fill("0.25");
      await expect(offsetBlock).toContainText("適用後: 先頭の拍 0.25 秒");
      await expect(offsetBlock).toContainText("最後の拍 179.75 秒");

      const bpmBlock = editor.locator("section", { hasText: "固定BPMへ上書き" });
      await bpmBlock.getByPlaceholder("例: 120").fill("60");
      await expect(bpmBlock).toContainText("適用後: 全ての拍間隔が 1.00 秒 になる");
    });

    await test.step("ビートにクリック音を重ねて再生できる", async () => {
      const clickToggle = viewer.getByLabel("ビートにクリック音(小節の頭は高く)");
      await expect(clickToggle).toBeChecked();

      await viewer.getByRole("button", { name: "再生" }).click();
      // 再生が始まるとボタンが「一時停止」になり、再生位置が進む。
      await expect(viewer.getByRole("button", { name: "一時停止" })).toBeVisible({
        timeout: 10_000,
      });
      // 再生を押した時点で原音を取りに行く(遅延読み込み)。
      expect(audioRequests).toBeGreaterThan(audioRequestsBeforePlay);
      const position = viewer.getByTestId("beat-position");
      await expect
        .poll(() => position.innerText(), { timeout: 15_000 })
        .not.toBe("再生位置 0.00 秒");

      await viewer.getByRole("button", { name: "一時停止" }).click();
      await expect(viewer.getByRole("button", { name: "再生" })).toBeVisible();

      // クリック音は入切できる。
      await clickToggle.uncheck();
      await expect(clickToggle).not.toBeChecked();
      await clickToggle.check();

      // 先頭へ戻せる。
      await viewer.getByRole("button", { name: "先頭へ" }).click();
      await expect(viewer.getByTestId("beat-position")).toHaveText("再生位置 0.00 秒");
    });
  } finally {
    await app.close();
  }
});
