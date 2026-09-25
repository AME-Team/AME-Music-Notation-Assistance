import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_QUANTIZE_SETTINGS,
  minValueLabel,
  normalizeMinValue,
  normalizeQuantizeSettings,
  normalizeStrengthPercent,
  quantizeSettingsSummary,
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
});
