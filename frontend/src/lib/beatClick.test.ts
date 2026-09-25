import { describe, expect, it } from "vitest";
import {
  beatIndexAtOrAfter,
  beatsInRange,
  CLICK_FREQ_HZ,
  clickFrequencyHz,
  downbeatTimeSet,
} from "./beatClick";

describe("beatClick", () => {
  const beats = [0.04, 0.54, 1.04, 1.54, 2.04];

  it("指定時刻以降で最初の拍を二分探索で返す", () => {
    expect(beatIndexAtOrAfter(beats, 0)).toBe(0);
    expect(beatIndexAtOrAfter(beats, 0.04)).toBe(0);
    // 拍と拍の間は「次の拍」を指す(先読み予約でちょうど良い)。
    expect(beatIndexAtOrAfter(beats, 0.05)).toBe(1);
    expect(beatIndexAtOrAfter(beats, 1.04)).toBe(2);
    expect(beatIndexAtOrAfter(beats, 1.5)).toBe(3);
  });

  it("末尾より後ろ・空配列では『見つからない』を表す添字を返す", () => {
    expect(beatIndexAtOrAfter(beats, 99)).toBe(beats.length);
    expect(beatIndexAtOrAfter([], 1)).toBe(0);
  });

  it("先読み区間に入る拍だけを取り出す", () => {
    expect(beatsInRange(beats, 0.5, 1.6)).toEqual([0.54, 1.04, 1.54]);
    // 区間の終端を含む。
    expect(beatsInRange(beats, 1.04, 1.54)).toEqual([1.04, 1.54]);
    expect(beatsInRange(beats, 3, 4)).toEqual([]);
  });

  it("ダウンビートは高いクリック音にする", () => {
    expect(clickFrequencyHz(true)).toBe(CLICK_FREQ_HZ.downbeat);
    expect(clickFrequencyHz(false)).toBe(CLICK_FREQ_HZ.beat);
    expect(CLICK_FREQ_HZ.downbeat).toBeGreaterThan(CLICK_FREQ_HZ.beat);
  });

  it("ダウンビートの集合は拍の時刻をそのままキーにする", () => {
    // 丸めると別の拍と衝突しうるため、JSON由来の数値をそのまま使う。
    const set = downbeatTimeSet([0.04, 2.04]);

    expect(set.has(0.04)).toBe(true);
    expect(set.has(0.54)).toBe(false);
  });
});
