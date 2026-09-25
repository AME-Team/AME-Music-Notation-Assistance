import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #172: 「先頭N小節のMIDIと楽譜」が**すべての作業ページ**に出ること、小節数が設定で
 * 変えられることの回帰テスト。
 *
 * 補正は実物のMIDIと楽譜を見ながら行う(アプリの根幹)。以前は補正できる②には楽譜も
 * MIDIも無く、数値を勘で入れるしかなかった。ここでは①②③…⑦の各ページでパネルが
 * 見えること、既定が4小節であること、8小節へ変えると表示が追従し保存されることを見る。
 *
 * ## 実装上の注意
 *
 * - `page.route()`によるAPIモックは、ElectronのCSPヘッダ注入と併用すると壊れる
 *   既知バグがあるため、`AME_E2E_DISABLE_CSP_HEADER`で注入を止める(#160で実測)。
 * - 各ステップを開けるよう、単一プロジェクト取得の応答だけを差し替えて全ステージを
 *   完了済みに見せかける(DSP推論は走らせない)。
 */

const SAMPLE_RATE = 8000;
const DURATION_SEC = 8;
const DIVISIONS = 480;
const BARS = 16;

/** 4/4・16小節、各小節に4分音符を4つ置いた決定的なScoreIR(量子化済み)。 */
function buildScore() {
  type Note = {
    id: number;
    midi: number;
    onset_tick: number;
    duration_tick: number;
    velocity: number;
    voice: number;
    staff: number;
    flags: string[];
  };
  const notes: Note[] = [];
  for (let bar = 0; bar < BARS; bar += 1) {
    for (let beat = 0; beat < 4; beat += 1) {
      notes.push({
        id: notes.length + 1,
        midi: 60 + (beat % 4),
        onset_tick: bar * 4 * DIVISIONS + beat * DIVISIONS,
        duration_tick: DIVISIONS,
        velocity: 80,
        voice: 1,
        staff: 1,
        flags: [],
      });
    }
  }
  return {
    schema_version: 1,
    project_id: "e2e",
    source: { filename: "preview.wav", duration_sec: DURATION_SEC, sample_rate: SAMPLE_RATE },
    divisions: DIVISIONS,
    tempo_map: [{ bar: 1, beat: 1, bpm: 120 }],
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    key_signatures: [],
    chords: [],
    parts: [{ id: "piano", name: "Piano", notes }],
    meta: { stages: {} },
    next_note_id: notes.length + 1,
  };
}

/** OSMDが描画できる最小のMusicXML(1パート・8小節の全音符)。 */
function buildMusicXml(): string {
  const measure = (bar: number) =>
    `<measure number="${bar}"><attributes><divisions>1</divisions>` +
    `<time><beats>4</beats><beat-type>4</beat-type></time></attributes>` +
    `<note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice>` +
    `<type>whole</type></note></measure>`;
  const measures = Array.from({ length: 8 }, (_, index) => measure(index + 1)).join("");
  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n<score-partwise version="3.1">\n' +
    '<part-list><score-part id="P1"><part-name>P</part-name></score-part></part-list>\n' +
    `<part id="P1">${measures}</part>\n</score-partwise>\n`
  );
}

/** 指定秒数の無音WAV(8kHz・モノラル・16bit)を書く。 */
function makeSilentWavFile(seconds: number): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "preview.wav");
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

test("first N bars of MIDI and score are visible on every work page (#172)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({
    args: [mainPath],
    env: { ...process.env, AME_E2E_DISABLE_CSP_HEADER: "1" },
  });
  const page = await app.firstWindow();
  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    await page.route("**/score/preview.musicxml", (route) =>
      route.fulfill({ status: 200, contentType: "application/xml", body: buildMusicXml() }),
    );
    await page.route(/\/api\/projects\/[^/]+\/score$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(buildScore()),
      }),
    );
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

    // 表示小節数はlocalStorageに残るため、前回実行の値を持ち越さないよう初期化する
    // (このアプリのウィンドウは仕様上リロードできる。#172の既定は4小節)。
    await page.evaluate(() => window.localStorage.removeItem("ame.scoreView.bars"));
    await page.reload();
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    await test.step("プロジェクトを作成する", async () => {
      await page.getByLabel("音声ファイルを選択").setInputFiles(makeSilentWavFile(DURATION_SEC));
      await expect(page.getByText("preview.wav")).toBeVisible({ timeout: 20_000 });
      await page.getByText("preview.wav").click();
    });

    const panel = page.getByTestId("score-view-panel");
    const steps = page.getByRole("button", { name: /^\d+\. / });

    await test.step("すべての作業ページでパネルが見えている", async () => {
      const count = await steps.count();
      expect(count).toBeGreaterThanOrEqual(7);
      for (let index = 0; index < count; index += 1) {
        await steps.nth(index).click();
        await expect(panel).toBeVisible();
        await expect(panel.getByTestId("midi-bar")).toBeVisible({ timeout: 15_000 });
        // 先頭4小節(既定)に音符が描かれている。
        expect(await panel.getByTestId("midi-note").count()).toBeGreaterThan(0);
        await expect(panel).toContainText("先頭4小節のプレビュー");
        // 小節番号は実在する小節の数だけ(線は右端の次小節線を含むため+1本)。
        expect(await panel.getByTestId("midi-bar-label").count()).toBe(4);
        expect(await panel.getByTestId("midi-barline").count()).toBe(5);
        // 楽譜(OSMD)も同じページに描画されている。見出しは埋め込み側で消しているため、
        // 実際に譜面が描かれたこと(SVG)を確かめる。
        await expect(panel.locator("div.bg-white svg").first()).toBeVisible({ timeout: 15_000 });
      }
    });

    await test.step("表示する小節数を8へ変えると追従して保存される", async () => {
      const before = await panel.getByTestId("midi-barline").count();
      await panel.getByLabel("表示する小節数").selectOption("8");
      await expect(panel).toContainText("先頭8小節のプレビュー");
      expect(await panel.getByTestId("midi-barline").count()).toBeGreaterThan(before);
      expect(await panel.getByTestId("midi-bar-label").count()).toBe(8);
      expect(await page.evaluate(() => localStorage.getItem("ame.scoreView.bars"))).toBe("8");
    });

    await test.step("パネルが全ページで閉じずに残っている(設定変更後も)", async () => {
      await steps.nth(0).click();
      await expect(panel).toContainText("先頭8小節のプレビュー");
    });
  } finally {
    await app.close();
  }
});
