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

  it("ダウンビートは拍の時刻へ許容誤差つきで対応付ける", () => {
    // 返す集合のキーは拍の時刻そのもの(呼び出し側は拍の時刻で引く)。
    const beatTimes = [0.04, 0.54, 1.04, 1.54, 2.04];
    const set = downbeatTimeSet(beatTimes, [0.04, 2.04]);

    expect(set.has(0.04)).toBe(true);
    expect(set.has(0.54)).toBe(false);
    // 保存・編集の経路が違って丸めがずれても、同じ拍をダウンビートとみなす
    // (#173レビュー指摘: 厳密一致に依存すると常に低い音になり無言で壊れる)。
    const shifted = downbeatTimeSet(beatTimes, [0.0400001, 2.039999]);
    expect(shifted.has(0.04)).toBe(true);
    expect(shifted.has(2.04)).toBe(true);
    // 隣の拍(0.54)はダウンビートではない。
    expect(downbeatTimeSet(beatTimes, [0.6]).size).toBe(0);
  });
});
