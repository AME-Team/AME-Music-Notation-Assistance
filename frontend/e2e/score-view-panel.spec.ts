import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { type ElectronApplication, _electron as electron, expect, test } from "@playwright/test";
// 実装と同じ知識を参照する(自前の譜面を持つステップの判定を二重管理しない)。
import { STEPS_WITH_OWN_SCORE } from "../src/lib/scoreView";
import type { StepId } from "../src/lib/workflow";

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
    // #174: バックエンドが量子化時に残す「実際に適用した設定」。
    meta: {
      stages: { quantize: { settings: { min_value: "1/16", strength: 1, enabled: true } } },
    },
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

/**
 * 指定秒数の無音WAV(8kHz・モノラル・16bit)を書く。
 *
 * `filename`を指定できるのは、同じ実行のワークスペースを共有する2つ目の
 * テストが、1つ目のテストのプロジェクト名と衝突しないようにするため。
 */
function makeSilentWavFile(seconds: number, filename = "preview.wav"): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, filename);
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
    await page.evaluate(() => {
      window.localStorage.removeItem("ame.scoreView.bars");
      // #179: 楽譜の拡大率も前回実行の値を持ち越さない(既定は100%)。
      window.localStorage.removeItem("ame.scoreView.zoom");
      // #181: 5線譜の並べ方も前回実行の値を持ち越さない(既定は横に1段)。
      window.localStorage.removeItem("ame.scoreView.layout");
      // #174: 量子化の設定も前回実行の値を持ち越さない。
      window.localStorage.removeItem("ame.quantize");
    });
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

        // 小節番号は実在する小節の数だけ(線は右端の次小節線を含むため+1本)。
        expect(await panel.getByTestId("midi-bar-label").count()).toBe(4);
        expect(await panel.getByTestId("midi-barline").count()).toBe(5);
        const stepId = ["separate", "beat", "transcribe", "quantize", "refine", "review", "export"][
          index
        ];
        // #179: 楽譜が出るページでは最上部のパネルに5線譜が出る(メインに据える)。
        // ⑤⑥は自前の譜面(差分表示)がステップ内にあるため、パネルはMIDIのみ
        // (同一画面でOSMDを二重に走らせない)。
        // 自前の譜面を持つステップでは見出しもMIDIのみを指す(実表示と一致させる)。
        // "MIDI(先頭4小節)" は "楽譜とMIDI(先頭4小節)" の部分文字列なので、
        // toContainTextでは分岐の正しさを検証できない。見出し要素へ完全一致で当てる。
        const expectsOwnScore = STEPS_WITH_OWN_SCORE.includes(stepId as StepId);
        await expect(panel.getByRole("heading", { level: 2 })).toHaveText(
          expectsOwnScore ? "MIDI(先頭4小節)" : "楽譜とMIDI(先頭4小節)",
        );
        if (expectsOwnScore) {
          await expect(panel).toContainText("ここはMIDIバーのみ表示します");
        } else {
          await expect(panel.locator("div.bg-white svg").first()).toBeVisible({ timeout: 15_000 });
        }
        // #174: 量子化の既定は16分音符・強さ100%・有効。現在値がその場に出る。
        await expect(panel.getByTestId("quantize-summary")).toContainText(
          "16分音符・強さ100%・クオンタイズON",
        );
        // ④を再実行してよいのは①〜④のページだけ(⑤以降の手動補正を上書きしない)。
        const rerun = panel.getByRole("button", { name: "ビート補正を反映して更新" });
        if (["separate", "beat", "transcribe", "quantize"].includes(stepId)) {
          await expect(rerun).toBeEnabled();
        } else {
          await expect(rerun).toBeDisabled();
          await expect(panel).toContainText("ここからは④を再実行できません");
        }
      }
    });

    await test.step("表示する小節数を8へ変えると追従して保存される", async () => {
      const before = await panel.getByTestId("midi-barline").count();
      await panel.getByLabel("表示する小節数").selectOption("8");
      await expect(panel).toContainText("楽譜とMIDI(先頭8小節)");
      expect(await panel.getByTestId("midi-barline").count()).toBeGreaterThan(before);
      expect(await panel.getByTestId("midi-bar-label").count()).toBe(8);
      expect(await page.evaluate(() => localStorage.getItem("ame.scoreView.bars"))).toBe("8");
    });

    await test.step("楽譜とMIDIがステップ操作より上に固定されている(#179)", async () => {
      // ②ビート(補正UIが長いページ)を開く。
      await steps.nth(1).click();
      const panelBox = await panel.boundingBox();
      const stepBox = await page.getByTestId("step-content").boundingBox();
      expect(panelBox).not.toBeNull();
      expect(stepBox).not.toBeNull();
      if (panelBox && stepBox) {
        // 楽譜とMIDIはステップの操作より上にある(「下の方にある」状態の回帰防止)。
        expect(panelBox.y).toBeLessThan(stepBox.y);
      }
      // 操作をスクロールしても楽譜とMIDIは画面内に残る(sticky)。
      await page.mouse.wheel(0, 1500);
      await expect(panel.getByTestId("midi-bar")).toBeVisible();
      await expect(panel.locator("div.bg-white svg").first()).toBeVisible();
    });

    await test.step("楽譜より下のセクションが隠れない(#181)", async () => {
      // ②ビート(補正UIが長いページ)を開く。
      await steps.nth(1).click();
      await expect(panel.getByTestId("midi-bar")).toBeVisible({ timeout: 15_000 });

      const geometry = await page.evaluate(() => {
        const scroller = document.scrollingElement ?? document.documentElement;
        const panelEl = document.querySelector('[data-testid="score-view-panel"]');
        const panelRect = panelEl?.getBoundingClientRect();
        return {
          innerHeight: window.innerHeight,
          panelHeight: Math.round(panelRect?.height ?? 0),
          canScroll: scroller.scrollHeight > scroller.clientHeight,
        };
      });
      // stickyなパネルが画面を占め切ると、下のセクションをどうスクロールしても
      // 見られなくなる(実測: viewport 770pxに対しパネル428pxが最上部に固定され、
      // 内容の先頭がパネルの下へ潜っていた)。
      expect(geometry.panelHeight).toBeLessThan(geometry.innerHeight * 0.8);

      // 最下部までスクロールしたとき、内容の末尾が画面内に入っていること。
      await page.evaluate(() => {
        const scroller = document.scrollingElement ?? document.documentElement;
        scroller.scrollTop = scroller.scrollHeight;
      });
      await page.waitForTimeout(200);
      const atBottom = await page.evaluate(() => {
        const rect = (el: Element | null) => {
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return { top: Math.round(r.top), bottom: Math.round(r.bottom) };
        };
        const content = document.querySelector('[data-testid="step-content"]');
        const last = content?.lastElementChild ?? null;
        const panelEl = document.querySelector('[data-testid="score-view-panel"]');
        return {
          innerHeight: window.innerHeight,
          panel: rect(panelEl),
          lastSection: rect(last),
          content: rect(content),
        };
      });
      expect(atBottom.lastSection).not.toBeNull();
      expect(atBottom.content).not.toBeNull();
      if (atBottom.lastSection && atBottom.panel && atBottom.content) {
        // 末尾が画面内(=一番下までスクロールすれば読める)。
        expect(atBottom.lastSection.bottom).toBeLessThanOrEqual(atBottom.innerHeight + 1);
        // パネルの下に内容が見えている(=パネルが画面を占め切っていない)。
        expect(atBottom.content.bottom).toBeGreaterThan(atBottom.panel.bottom);
      }
    });

    await test.step("5線譜は1本の横方向の段に並び、ズームが反映される(#179)", async () => {
      const canvas = panel.getByTestId("score-preview-canvas");
      // 5線譜を横に並べる設定がOSMDインスタンスへ渡っている。
      await expect(canvas).toHaveAttribute("data-single-horizontal-staffline", "true");
      await expect(canvas).toHaveAttribute("data-zoom", "1");

      // ズームを上げると実際の描画(SVG)が拡大する=再描画に反映されている。
      const widthAt100 = await canvas
        .locator("svg")
        .first()
        .evaluate((el) => el.getBoundingClientRect().width);
      await panel.getByLabel("楽譜の縮尺").fill("200");
      await expect(panel.getByTestId("score-zoom")).toHaveText("200%");
      await expect(canvas).toHaveAttribute("data-zoom", "2");
      const widthAt200 = await canvas
        .locator("svg")
        .first()
        .evaluate((el) => el.getBoundingClientRect().width);
      expect(widthAt200).toBeGreaterThan(widthAt100);
      // 保存され、リロード後も維持される。
      expect(await page.evaluate(() => localStorage.getItem("ame.scoreView.zoom"))).toBe("2");

      // 5線譜が表示領域より横に広い=折り返して縦に積まれていない。
      const sizes = await canvas.evaluate((el) => ({
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
      }));
      expect(sizes.scrollWidth).toBeGreaterThan(sizes.clientWidth);

      // 並べ方を「幅に合わせる」へ変えると、折り返して表示領域に収まる(#181)。
      await panel.getByLabel("5線譜の並べ方").selectOption("wrap");
      await expect(panel.getByTestId("score-preview-canvas")).toHaveAttribute(
        "data-single-horizontal-staffline",
        "false",
      );
      await expect
        .poll(async () => canvas.evaluate((el) => el.scrollWidth - el.clientWidth))
        .toBeLessThanOrEqual(1);
    });

    await test.step("パネルが全ページで閉じずに残っている(設定変更後も)", async () => {
      await steps.nth(0).click();
      await expect(panel).toContainText("楽譜とMIDI(先頭8小節)");
    });
  } finally {
    await app.close();
  }
});

/** ネイティブメニューの「編集 > 設定」を実行する(mainプロセス側で操作)。 */
async function clickSettingsMenuItem(app: ElectronApplication): Promise<void> {
  await app.evaluate(({ Menu }) => {
    const edit = Menu.getApplicationMenu()?.items.find((item) => item.label === "編集");
    const settings = edit?.submenu?.items.find((item) => item.label === "設定");
    if (!settings) throw new Error("ネイティブメニューに 編集 > 設定 が見つかりません");
    settings.click();
  });
}

test("quantize settings (min note value, strength, on/off) reach the panel and the job (#174)", async () => {
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

    // #174: 量子化の設定もlocalStorageに残るため、前回実行の値を持ち越さない。
    await page.evaluate(() => {
      window.localStorage.removeItem("ame.quantize");
      window.localStorage.removeItem("ame.scoreView.bars");
    });
    await page.reload();
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    const panel = page.getByTestId("score-view-panel");

    await test.step("プロジェクトを作成して①を開く", async () => {
      await page
        .getByLabel("音声ファイルを選択")
        .setInputFiles(makeSilentWavFile(DURATION_SEC, "settings.wav"));
      await expect(page.getByText("settings.wav")).toBeVisible({ timeout: 20_000 });
      // リロード後は前回のプロジェクトが開いた状態で戻ることがあるため、
      // パネルが出ていなければ一覧からプロジェクトを開く。
      if (!(await panel.isVisible())) {
        await page.getByText("settings.wav").first().click();
      }
      await page
        .getByRole("button", { name: /^\d+\. / })
        .nth(0)
        .click();
      await expect(panel).toBeVisible({ timeout: 20_000 });
    });

    await test.step("設定モーダルで最小音符単位・強さ・入切を変える", async () => {
      await clickSettingsMenuItem(app);
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await dialog.getByTestId("quantize-strength").fill("40");
      await dialog.getByLabel("32分音符").check();
      await dialog.getByTestId("quantize-enabled").check();
      await expect(dialog.getByTestId("quantize-strength-value")).toHaveText("40%");
      await dialog.getByRole("button", { name: "閉じる" }).click();
      await expect(dialog).toBeHidden();
    });

    await test.step("パネルが現在の量子化設定を表示する", async () => {
      // 保存値(次回の実行)と、スコアに記録された適用済みの設定を出し分ける。
      await expect(panel.getByTestId("quantize-summary")).toContainText(
        "適用中: 16分音符・強さ100%・クオンタイズON / 次回の実行: 32分音符・強さ40%・クオンタイズON",
      );
    });

    await test.step("設定はリロード後も維持される", async () => {
      await page.reload();
      await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({
        timeout: 30_000,
      });
      await page.getByText("settings.wav").click();
      await page
        .getByRole("button", { name: /^\d+\. / })
        .nth(0)
        .click();
      await expect(panel.getByTestId("quantize-summary")).toContainText(
        "次回の実行: 32分音符・強さ40%・クオンタイズON",
      );
    });

    await test.step("④の再実行が設定をパラメータで送る", async () => {
      let posted: { params?: Record<string, unknown> } | null = null;
      await page.route("**/stages/quantize/run", async (route) => {
        posted = JSON.parse(route.request().postData() ?? "{}");
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ job_id: "job_settings" }),
        });
      });
      await panel.getByRole("button", { name: "ビート補正を反映して更新" }).click();
      await expect.poll(() => posted).not.toBeNull();
      expect(posted?.params).toMatchObject({
        quantize_min_value: "1/32",
        quantize_strength: 0.4,
        quantize_enabled: true,
      });
    });
  } finally {
    await app.close();
  }
});
