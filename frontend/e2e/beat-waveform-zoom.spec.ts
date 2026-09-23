import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #169: 「②テンポ・拍の検出」の波形ビューが**ズームと横スクロール**に対応し、
 * 全曲を横幅へ圧縮しないことの回帰テスト。
 *
 * 以前は3分程度の曲でも波形を全幅に圧縮していたため、1拍が数ピクセルになり、
 * 波形を見ながら全体オフセットを決められなかった。ここでは実アプリを起動し、
 *
 * - 拡大すると波形とビート線が**同じ表示区間**になること(viewBoxの一致)
 * - 横スクロールで表示開始位置が動くこと
 * - 拡大時に高解像度のピークを要求すること(解像度クエリ)
 * - 全体オフセットの微調整ボタンが差分オフセットを送ること
 *
 * を検証する。
 *
 * ## 実装上の注意
 *
 * - `page.route()`によるAPIモックは、ElectronのCSPヘッダ注入と併用すると壊れる
 *   既知バグがあるため、`AME_E2E_DISABLE_CSP_HEADER`で注入を止める(#160で実測)。
 * - ②は①が完了扱いでないと選択できないため、単一プロジェクト取得の応答だけを
 *   差し替えて①〜④を完了済みに見せかける。DSP推論は走らせない。
 * - ズーム/スクロールの検証には「曲長に対して十分長い」素材が要るため、
 *   3分(180秒)の無音WAVを作る(app.spec.tsの最小WAVは0.1秒で、拡大の余地が無い)。
 */

/** 検証に使う曲長(秒)。3分だと全曲表示では1拍が数ピクセルになる、という前提を再現する。 */
const DURATION_SEC = 180;
const SAMPLE_RATE = 8000;

/** 4/4・120 BPM相当(各拍0.5秒)の決定的なbeatmap。 */
function buildBeatmap() {
  const beatCount = 360;
  const beats = [];
  const downbeats: number[] = [];
  for (let index = 0; index < beatCount; index += 1) {
    const bar = Math.floor(index / 4) + 1;
    const beatInBar = (index % 4) + 1;
    const timeSec = Number((index * 0.5).toFixed(2));
    beats.push({ time_sec: timeSec, beat_in_bar: beatInBar, bar });
    if (beatInBar === 1) downbeats.push(timeSec);
  }

  return {
    beats,
    downbeats_sec: downbeats,
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    // `tempo_map`は先頭の拍を含まない(backend/app/pipeline/beat.pyの`_tempo_map`)。
    tempo_map: beats.slice(1).map((beat) => ({ bar: beat.bar, beat: beat.beat_in_bar, bpm: 120 })),
    confidence: 0.99,
    source: "auto",
  };
}

/** 指定秒数の無音WAV(8kHz・モノラル・16bit)を書く。 */
function makeSilentWavFile(seconds: number): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "long-song.wav");
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

test("beat waveform viewer supports zoom and horizontal scroll (#169)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({
    args: [mainPath],
    env: { ...process.env, AME_E2E_DISABLE_CSP_HEADER: "1" },
  });
  const page = await app.firstWindow();

  /** ピークの要求URL(解像度クエリの検証用)。 */
  const peaksRequests: string[] = [];
  /** 補正リクエストのボディ(微調整ボタンの検証用)。 */
  const patchBodies: string[] = [];

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    await page.route("**/analysis/peaks/original*", (route) => {
      peaksRequests.push(route.request().url());
      return route.continue();
    });
    await page.route("**/analysis/beatmap", (route) => {
      if (route.request().method() === "PATCH") {
        patchBodies.push(route.request().postData() ?? "");
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(buildBeatmap()),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(buildBeatmap()),
      });
    });
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

    await test.step("プロジェクトを作成して②テンポ・拍の検出を開く", async () => {
      await page.getByLabel("音声ファイルを選択").setInputFiles(makeSilentWavFile(DURATION_SEC));
      await expect(page.getByText("long-song.wav")).toBeVisible({ timeout: 20_000 });
      await page.getByText("long-song.wav").click();
      await page.getByRole("button", { name: "2. テンポ・拍の検出" }).click();
      await expect(page.getByRole("heading", { name: "2. テンポ・拍の検出" })).toBeVisible();
    });

    const viewer = page.locator("section", { hasText: "波形とビートグリッド" });
    const svgs = viewer.locator("svg");
    const scrollbar = viewer.getByLabel("横スクロール");

    await test.step("初期表示は全曲(倍率1.0×)で、波形とビート線が同じ区間", async () => {
      await expect(viewer.getByText("倍率 1.0×")).toBeVisible({ timeout: 30_000 });
      await expect(viewer.getByText("波形の粒度 180 ms/点")).toBeVisible();
      // 波形(viewBoxの高さ2)とビート線(高さ1)で、開始位置と表示幅が一致していること。
      await expect(svgs.nth(0)).toHaveAttribute("viewBox", "0 0 180 2");
      await expect(svgs.nth(1)).toHaveAttribute("viewBox", "0 0 180 1");
      // 全体表示では動かす余地が無い。
      await expect(scrollbar).toBeDisabled();
    });

    await test.step("ズームインで表示幅が縮み、波形とビート線が揃って拡大される", async () => {
      await viewer.getByRole("button", { name: "ズームイン" }).click();

      // 180 / 1.5 = 120秒を、中央(90秒)を固定して表示する。
      await expect(viewer.getByText("倍率 1.5×")).toBeVisible();
      await expect(viewer.getByText("表示幅 120.00 秒")).toBeVisible();
      await expect(svgs.nth(0)).toHaveAttribute("viewBox", "30 0 120 2");
      await expect(svgs.nth(1)).toHaveAttribute("viewBox", "30 0 120 1");
      await expect(scrollbar).toBeEnabled();
    });

    await test.step("横スクロールで表示開始位置が動く", async () => {
      await scrollbar.fill("1000");

      // 180 - 120 = 60秒が最右端。
      await expect(svgs.nth(0)).toHaveAttribute("viewBox", "60 0 120 2");
      await expect(svgs.nth(1)).toHaveAttribute("viewBox", "60 0 120 1");

      await scrollbar.fill("0");
      await expect(svgs.nth(0)).toHaveAttribute("viewBox", "0 0 120 2");
    });

    await test.step("拡大すると高解像度のピークを要求する", async () => {
      // 2回目のズームで表示幅80秒(中央60秒固定 → 20〜100秒)になる。
      await viewer.getByRole("button", { name: "ズームイン" }).click();
      await expect(viewer.getByText("表示幅 80.00 秒")).toBeVisible();
      await expect(svgs.nth(0)).toHaveAttribute("viewBox", "20 0 80 2");
      await expect(svgs.nth(1)).toHaveAttribute("viewBox", "20 0 80 1");

      await expect
        .poll(() =>
          peaksRequests.some((url) => Number(new URL(url).searchParams.get("buckets")) > 1000),
        )
        .toBe(true);
      // 解像度が上がったことは画面の粒度表示からも分かる(180ms/点 → より細かい値)。
      await expect(viewer.getByText("波形の粒度 180 ms/点")).toHaveCount(0);
    });

    await test.step("全体表示へ戻せる", async () => {
      await viewer.getByRole("button", { name: "全体表示" }).click();

      await expect(viewer.getByText("倍率 1.0×")).toBeVisible();
      await expect(svgs.nth(0)).toHaveAttribute("viewBox", "0 0 180 2");
      await expect(scrollbar).toBeDisabled();
    });

    await test.step("全体オフセットを微調整ボタンで加算できる", async () => {
      await page.getByRole("button", { name: "+10 ms" }).click();

      await expect.poll(() => patchBodies.length).toBeGreaterThan(0);
      expect(JSON.parse(patchBodies[0])).toMatchObject({ offset_sec: 0.01 });

      await page.getByRole("button", { name: "-50 ms" }).click();
      await expect.poll(() => patchBodies.length).toBeGreaterThan(1);
      expect(JSON.parse(patchBodies[1])).toMatchObject({ offset_sec: -0.05 });
    });
  } finally {
    await app.close();
  }
});
