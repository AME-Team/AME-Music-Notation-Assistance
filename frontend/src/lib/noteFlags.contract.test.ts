/**
 * #142レビュー指摘: フロントエンドが表示に使うフラグ名は、バックエンド(L0)が
 * `Note.flags`へ書き込む文字列と一致していなければ、**例外も出ずに可視化が
 * 黙って無効化される**(型では表現できないクロス言語の契約)。
 *
 * そこでバックエンドのソースから定数定義を読んで突き合わせる。バックエンド側の
 * 定義(`backend/app/pipeline/refine/baseline.py`の`VOICE_SATURATION_FLAG`)を
 * 変更する場合はこのテストが落ちるので、フロント側の`SATURATED_FLAG`も同時に直すこと。
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { GHOST_FLAG, SATURATED_FLAG } from "./pianoRoll";

const BASELINE_PY = new URL("../../../backend/app/pipeline/refine/baseline.py", import.meta.url);
const DSP_MAIN_PY = new URL("../../../backend/app/worker/dsp_main.py", import.meta.url);

/** `NAME = "value"` 形式のモジュールレベル定数を取り出す(見つからなければ明示的に失敗)。 */
function backendConstant(source: string, name: string): string {
  const match = source.match(new RegExp(`^${name}\\s*=\\s*"([^"]+)"`, "m"));
  if (!match) {
    throw new Error(`backend の定数 ${name} が見つかりません(定義場所が変わった可能性)`);
  }
  return match[1];
}

describe("note flag contract with the backend (#142)", () => {
  it("matches VOICE_SATURATION_FLAG in pipeline/refine/baseline.py", () => {
    const source = readFileSync(BASELINE_PY, "utf-8");
    expect(backendConstant(source, "VOICE_SATURATION_FLAG")).toBe(SATURATED_FLAG);
  });

  it("keeps the ghost flag used by the worker in sync", () => {
    // ghostは定数化されておらず`dsp_main.py`に文字列リテラルで書かれているため、
    // 出現の有無だけを確認する(2026-09時点)。
    const source = readFileSync(DSP_MAIN_PY, "utf-8");
    expect(source).toContain(`"${GHOST_FLAG}"`);
  });
});
