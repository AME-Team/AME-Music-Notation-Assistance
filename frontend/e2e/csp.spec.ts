import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron, expect, test } from "@playwright/test";

// package.json の "type": "module" により __dirname は使えないため import.meta.url から導出する。
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * #60(Windows実機検証)で発覚したCSPバグの回帰テスト。
 *
 * Electronのメインプロセス(`electron/main.ts`)はレンダラーの応答ヘッダに
 * `Content-Security-Policy` を上書きしている。`worker-src`を指定していなかった
 * ため、Tone.js(再生機能 #34)が作るBlob Workerが`script-src`(blob:を含まない)へ
 * フォールバックしてブロックされていた。
 *
 * このとき`new Worker()`は**例外を投げない**(生成だけ成功し、onerrorで無言で死ぬ)
 * ため、Tone側のtimeoutフォールバックも働かず「contextのtickイベントが一度も
 * 発火しない」状態になっていた(実ブラウザ計測でticks=0)。アプリを実際に起動して
 * **Blob Workerが動くこと**を確認する。
 *
 * なお、通常の`npm test`(vitest)やブラウザでの開発時はこのCSPヘッダが付かない
 * ため検出できず、Electronとして起動するこのe2eでしか捕まえられない。
 */
test("blob Worker is allowed by the app CSP (Tone.js transport clock)", async () => {
  const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
  const app = await electron.launch({ args: [mainPath] });
  const page = await app.firstWindow();

  const cspViolations: string[] = [];
  page.on("console", (message) => {
    const text = message.text();
    if (text.includes("Content Security Policy")) cspViolations.push(text);
  });

  await expect(page.getByText("AME Music Notation Assistance")).toBeVisible({ timeout: 30_000 });

  // Tone.jsのTickerと同じ手順(Blob → createObjectURL → new Worker)で、
  // 実際にメッセージが届く(=クロックが動く)ことを確認する。
  const ticks = await page.evaluate(async () => {
    const blob = new Blob(
      ["self.postMessage('tick'); setInterval(() => self.postMessage('tick'), 20);"],
      { type: "text/javascript" },
    );
    const worker = new Worker(URL.createObjectURL(blob));
    let count = 0;
    worker.onmessage = () => {
      count += 1;
    };
    await new Promise((resolve) => setTimeout(resolve, 400));
    worker.terminate();
    return count;
  });

  expect(ticks).toBeGreaterThan(0);
  expect(cspViolations).toEqual([]);

  await app.close();
});