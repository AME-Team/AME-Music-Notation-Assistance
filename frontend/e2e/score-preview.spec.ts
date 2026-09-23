import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #160: 譜面プレビューの描画が失敗しても**アプリ全体が落ちない**ことの回帰テスト。
 *
 * Windows実機のログ:
 * ```
 * [ERROR] [renderer] Uncaught TypeError: Cannot read properties of undefined (reading 'hide')
 *   (http://localhost:5173/src/components/ScorePreview.tsx:107)
 * [WARN]  [renderer] An error occurred in the <ScorePreview> component.
 * ```
 *
 * 原因は、OSMDの`cursor`が**`render()`成功まで存在しない**こと。描画が例外になった状態でも
 * `loaded`は真のままなので、再生状態に追従するeffectの`osmd.cursor.hide()`が例外になり、
 * effect内の例外がReactツリー全体のアンマウント(=アプリがエラー画面)につながっていた。
 *
 * ここでは**描画で例外になるMusicXML**(パート間で小節数がずれたファイル。OSMD実測で
 * `Cannot read properties of undefined (reading 'parent')` を投げ、`cursor`は未定義のまま)を
 * `GET /score/preview.musicxml`応答として返し、アプリが生存したままエラーを表示することを確認する。
 *
 * ## `AME_E2E_DISABLE_CSP_HEADER` について
 *
 * `page.route()`でのモックは、Electronの`session.webRequest.onHeadersReceived`
 * (CSPヘッダ注入、`electron/main.ts`)が**同時に使われていると全て`status: 0`に
 * 壊れる**、Playwright側の既知の未修正バグ(実測で確認、upstream:
 * https://github.com/microsoft/playwright/issues/30495 、closed・plannedなし)。
 * コメント欄でも同じ回避策が取られている(「テスト中はonHeadersReceivedの
 * 呼び出し自体を省く」)。このテストのみ環境変数でCSPヘッダ注入を無効化する
 * (`electron/main.ts`参照)。CSP自体の回帰は`e2e/csp.spec.ts`が別途検証している
 * ため、ここで無効化してもCSPの検証カバレッジは失われない。
 */

/** OSMDの描画を失敗させるMusicXML(P1=5小節 / P2=1小節)。 */
// 通常の文字列リテラル(JSON.stringify相当)として持つ。テンプレートリテラルだと
// 埋め込みの`${`やバッククォートのエスケープが必要になり壊れやすいため。
const UNRENDERABLE_MUSICXML =
  '<?xml version="1.0" encoding="UTF-8"?>\n<score-partwise version="3.1">\n<part-list><score-part id="P1"><part-name>P</part-name></score-part><score-part id="P2"><part-name>B</part-name></score-part></part-list>\n<part id="P1"><measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note></measure>\n<measure number="2"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note></measure>\n<measure number="3"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note></measure>\n<measure number="4"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note></measure>\n<measure number="5"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note></measure>\n</part>\n<part id="P2"><measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note></measure>\n</part></score-partwise>\n';

/**
 * プレビューを描画する経路まで到達させるための最小ScoreIR(バックエンド応答を差し替える)。
 * `frontend/src/api/client.ts`の`ScoreIR`/`ScorePart`/`ScoreNote`が要求する
 * フィールドを全て埋める(省略すると`TransportBar`等の別コンポーネントが
 * `undefined`参照で落ち、このテストが検証したい対象と無関係な失敗になる)。
 */
const MINIMAL_SCORE = {
  schema_version: 1,
  project_id: "dummy",
  source: { filename: "smoke-test.wav", duration_sec: 2, sample_rate: 8000 },
  divisions: 480,
  time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
  key_signatures: [],
  tempo_map: [{ bar: 1, beat: 1, bpm: 120 }],
  chords: [],
  next_note_id: 2,
  meta: { stages: {} },
  parts: [
    {
      id: "piano",
      name: "Piano",
      midi_program: 0,
      stem_source: null,
      staves: 2,
      clefs: [
        { staff: 1, sign: "G", line: 2 },
        { staff: 2, sign: "F", line: 4 },
      ],
      notes: [
        {
          id: 1,
          onset_sec: 0,
          duration_sec: 0.5,
          onset_tick: 0,
          duration_tick: 480,
          midi: 60,
          velocity: 90,
          spelling: { step: "C", alter: 0, octave: 4 },
          voice: 1,
          staff: 1,
          tie: { start: false, stop: false },
          confidence: 1,
          provenance: "amt",
          provenance_run_id: null,
          status: "present",
          flags: [],
          snap_candidates: [],
          selected_snap: null,
          ai_reason: null,
        },
      ],
      pedals: [],
    },
  ],
};

/** app.spec.ts と同じ最小WAV(プロジェクトを作るためだけに使う)。 */
function makeTinyWavFile(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "ame-e2e-"));
  const filePath = path.join(dir, "smoke-test.wav");
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

test("score preview keeps the app alive when the sheet fails to render (#160)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({
    args: [mainPath],
    env: { ...process.env, AME_E2E_DISABLE_CSP_HEADER: "1" },
  });
  const page = await app.firstWindow();

  try {
    await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

    // 譜面の応答を差し替える: 描画で例外になるMusicXML と、それを要求させる最小スコア。
    await page.route("**/score/preview.musicxml", (route) =>
      route.fulfill({ status: 200, contentType: "application/xml", body: UNRENDERABLE_MUSICXML }),
    );
    await page.route(/\/api\/projects\/[^/]+\/score$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MINIMAL_SCORE),
      }),
    );
    // UI刷新: 楽譜プレビュー(PianoRollEditor経由)はワークフローの⑥確認・編集
    // ステップの中にあり、④リズム補正までが完了扱いでないと到達できない
    // (ステップのロック)。このテストは実際にDSPジョブを走らせずに描画失敗の
    // 経路だけを検証したいため、単一プロジェクト取得(`useProject`)の応答を
    // 差し替えて①〜④が完了済みであるかのように見せかける。
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

    // プロジェクトを作って開く。
    await page.getByLabel("音声ファイルを選択").setInputFiles(makeTinyWavFile());
    await expect(page.getByText("smoke-test.wav")).toBeVisible({ timeout: 10_000 });
    await page.getByText("smoke-test.wav").click();

    // ④まで完了扱いにしたことで⑥確認・編集がロック解除されている(上のroute参照)。
    // ここで開くと、その中のPianoRollEditor経由で譜面プレビューが描画される。
    await page.getByRole("button", { name: /確認・編集/ }).click();

    // 描画は失敗し、その理由が「楽譜プレビュー」セクション内に表示される(例外は
    // 外へ漏れない)。メッセージ文言はOSMD内部の例外(`ScorePreview.tsx`の
    // `catch`で`err.message`をそのまま表示している)のため、バージョンアップで
    // 変わりうる文言そのものに依存しないよう、エラー表示用の要素(赤字の`<p>`)が
    // 現れることだけを検証する(precommitレビュー指摘、LOW)。
    const previewSection = page.locator("section", { hasText: "楽譜プレビュー" });
    const previewError = previewSection.locator("p.text-red-600");
    await expect(previewError).toBeVisible({ timeout: 20_000 });
    await expect(previewError).not.toBeEmpty();

    // アプリは生きている(#160の修正前はここでReactツリーごとアンマウントされていた)。
    // UI刷新でプロジェクト選択中はヘッダーがプロジェクト名表示に変わるため、
    // 「プロジェクト一覧に戻る」ボタン(常設)の健在で生存を確認する。
    await expect(page.getByRole("button", { name: /プロジェクト一覧/ })).toBeVisible();
    await expect(page.getByText("楽譜プレビュー")).toBeVisible();
  } finally {
    await app.close();
  }
});
