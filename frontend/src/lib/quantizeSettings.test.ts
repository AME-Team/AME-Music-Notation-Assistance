import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it } from "vitest";
import {
  appliedQuantizeSettings,
  DEFAULT_QUANTIZE_SETTINGS,
  minValueLabel,
  normalizeMinValue,
  normalizeQuantizeSettings,
  normalizeStrengthPercent,
  QUANTIZE_MIN_VALUE_CHOICES,
  quantizeSettingsSummary,
  quantizeStatusLine,
  readStoredQuantizeSettings,
  storeQuantizeSettings,
  toStageParams,
} from "./quantizeSettings";

/** vitestの`src/lib`はnode環境なので、`window`の代わりに最小のStorageを作る。 */
function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
    clear: () => {
      map.clear();
    },
    key: (index: number) => [...map.keys()][index] ?? null,
    get length() {
      return map.size;
    },
  };
}

describe("量子化の適用設定(#174)", () => {
  let storage: Storage;

  beforeEach(() => {
    storage = fakeStorage();
  });

  it("既定は16分音符・強さ100%・有効", () => {
    expect(DEFAULT_QUANTIZE_SETTINGS).toEqual({
      minValue: "1/16",
      strengthPercent: 100,
      enabled: true,
    });
    expect(readStoredQuantizeSettings(storage)).toEqual(DEFAULT_QUANTIZE_SETTINGS);
  });

  it("未知の最小音符単位は既定へ寄せる", () => {
    expect(normalizeMinValue("1/16")).toBe("1/16");
    expect(normalizeMinValue("1/3")).toBe("1/16");
    expect(normalizeMinValue(undefined)).toBe("1/16");
    expect(normalizeMinValue(8)).toBe("1/16");
  });

  it("強さは0〜100の整数へ丸める", () => {
    expect(normalizeStrengthPercent(60.4)).toBe(60);
    expect(normalizeStrengthPercent("50")).toBe(50);
    expect(normalizeStrengthPercent(150)).toBe(100);
    expect(normalizeStrengthPercent(-10)).toBe(0);
    expect(normalizeStrengthPercent(Number.NaN)).toBe(100);
  });

  it("壊れた保存値は既定へ戻す", () => {
    expect(normalizeQuantizeSettings(null)).toEqual(DEFAULT_QUANTIZE_SETTINGS);
    expect(normalizeQuantizeSettings({ minValue: "1/3", strengthPercent: "x" })).toEqual(
      DEFAULT_QUANTIZE_SETTINGS,
    );
    storage.setItem("ame.quantize", "{壊れたJSON");
    expect(readStoredQuantizeSettings(storage)).toEqual(DEFAULT_QUANTIZE_SETTINGS);
  });

  it("保存して読み戻せる", () => {
    storeQuantizeSettings(storage, {
      minValue: "1/8",
      strengthPercent: 50,
      enabled: false,
    });
    expect(readStoredQuantizeSettings(storage)).toEqual({
      minValue: "1/8",
      strengthPercent: 50,
      enabled: false,
    });
  });

  it("ジョブのパラメータはバックエンドのキー名で、強さは0〜1へ変換する", () => {
    expect(toStageParams(DEFAULT_QUANTIZE_SETTINGS)).toEqual({
      quantize_min_value: "1/16",
      quantize_strength: 1,
      quantize_enabled: true,
    });
    expect(toStageParams({ minValue: "1/32", strengthPercent: 25, enabled: false })).toEqual({
      quantize_min_value: "1/32",
      quantize_strength: 0.25,
      quantize_enabled: false,
    });
  });

  it("現在値の要約を出す(OFFのときは強さを出さない)", () => {
    expect(minValueLabel("1/16")).toBe("16分音符");
    expect(quantizeSettingsSummary(DEFAULT_QUANTIZE_SETTINGS)).toBe(
      "16分音符・強さ100%・クオンタイズON",
    );
    expect(quantizeSettingsSummary({ minValue: "1/8", strengthPercent: 50, enabled: false })).toBe(
      "8分音符・クオンタイズOFF(生の演奏位置)",
    );
  });

  it("スコアの meta.stages から「適用済み」の設定を読む", () => {
    const score = {
      meta: {
        stages: {
          quantize: { settings: { min_value: "1/32", strength: 0.5, enabled: true } },
        },
      },
    };
    expect(appliedQuantizeSettings(score)).toEqual({
      minValue: "1/32",
      strengthPercent: 50,
      enabled: true,
    });
    // 記録が無い/壊れている場合は「適用中」と言わない(存在しない設定を主張しない)。
    expect(appliedQuantizeSettings({ meta: { stages: {} } })).toBeNull();
    expect(appliedQuantizeSettings({})).toBeNull();
    expect(
      appliedQuantizeSettings({
        meta: { stages: { quantize: { settings: { min_value: "1/16" } } } },
      }),
    ).toBeNull();
    expect(appliedQuantizeSettings({ meta: { stages: { quantize: "x" } } })).toBeNull();
  });

  it("適用済みと次回の実行を出し分ける", () => {
    const pending = { minValue: "1/32", strengthPercent: 40, enabled: true } as const;
    const applied = { minValue: "1/16", strengthPercent: 100, enabled: true } as const;
    expect(quantizeStatusLine(null, pending)).toBe("次回の実行: 32分音符・強さ40%・クオンタイズON");
    expect(quantizeStatusLine(applied, pending)).toBe(
      "適用中: 16分音符・強さ100%・クオンタイズON / 次回の実行: 32分音符・強さ40%・クオンタイズON",
    );
    expect(quantizeStatusLine(applied, applied)).toBe("適用中: 16分音符・強さ100%・クオンタイズON");
  });

  it("バックエンドの定義と食い違っていない(契約テスト)", () => {
    // 最小音符単位の選択肢とパラメータのキー名は、バックエンドのソースと一致して
    // いなければならない(片方だけ変えると送信値が未知の単位として弾かれる/設定が
    // 黙って無視される。LOWレビュー指摘)。
    const repoRoot = new URL("../../../", import.meta.url);
    const quantizePy = readFileSync(new URL("backend/app/pipeline/quantize.py", repoRoot), "utf8");
    const dspPy = readFileSync(new URL("backend/app/worker/dsp_main.py", repoRoot), "utf8");

    const block = /_MIN_VALUE_DIVISOR: dict\[str, int\] = \{([\s\S]*?)\n\}/.exec(quantizePy);
    // 定義が見つからないときに黙って通さない(定数が移動したら「無い」ことで
    // 通ってしまうテストにしない)。
    expect(block, "_MIN_VALUE_DIVISOR がバックエンドで見つかりません").not.toBeNull();
    const backendChoices = [...(block?.[1] ?? "").matchAll(/"([^"]+)":/g)].map((match) => match[1]);
    expect([...backendChoices].sort()).toEqual([...QUANTIZE_MIN_VALUE_CHOICES].sort());

    for (const key of Object.keys(toStageParams(DEFAULT_QUANTIZE_SETTINGS))) {
      expect(dspPy.includes(`"${key}"`), `${key} が dsp_main.py にありません`).toBe(true);
    }
  });
});
