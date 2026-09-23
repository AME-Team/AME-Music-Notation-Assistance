import { describe, expect, it } from "vitest";
import {
  clampView,
  DEFAULT_PEAKS_BUCKETS,
  fullView,
  MAX_REQUESTED_BUCKETS,
  MIN_VIEW_SPAN_SEC,
  panFraction,
  panView,
  pointDurationSec,
  requestedBuckets,
  rulerTicks,
  viewFromFraction,
  viewSpanSec,
  visiblePointRange,
  zoomFactor,
  zoomView,
} from "./waveformView";

describe("waveformView", () => {
  it("全曲表示の区間は0〜曲長で、倍率は1.0", () => {
    const view = fullView(180);

    expect(view).toEqual({ startSec: 0, endSec: 180 });
    expect(viewSpanSec(view)).toBe(180);
    expect(zoomFactor(view, 180)).toBe(1);
  });

  it("曲長が最小表示幅より短くても、潰れた区間にはしない", () => {
    // 0.1秒の素材で区間が0.1秒になると、以降のclampで幅が0に潰れて波形が出なくなる。
    expect(fullView(0.1).endSec).toBe(MIN_VIEW_SPAN_SEC);
  });

  it("表示幅は最小値と曲長の間、開始位置は曲の範囲内にクランプする", () => {
    expect(clampView({ startSec: 0, endSec: 0.1 }, 180)).toEqual({
      startSec: 0,
      endSec: MIN_VIEW_SPAN_SEC,
    });
    // 右端を超える区間は末尾に寄せる。
    expect(clampView({ startSec: 179.9, endSec: 180.5 }, 180)).toEqual({
      startSec: 179.4,
      endSec: 180,
    });
    // 負の開始位置は0に寄せ、表示幅(10秒)は保つ。
    expect(clampView({ startSec: -5, endSec: 5 }, 180)).toEqual({ startSec: 0, endSec: 10 });
  });

  it("表示中央を固定した拡大では、中央の時刻が動かない", () => {
    const view = zoomView({ startSec: 0, endSec: 180 }, 1.5, 90, 180);

    expect(viewSpanSec(view)).toBeCloseTo(120, 6);
    expect(view.startSec).toBeCloseTo(30, 6);
    expect(view.endSec).toBeCloseTo(150, 6);
    expect(zoomFactor(view, 180)).toBeCloseTo(1.5, 6);
  });

  it("ホイール位置を固定した拡大では、その時刻が同じ相対位置に残る", () => {
    // 60〜120秒を表示中に、90秒(=中央)を固定して2倍に拡大する。
    const view = zoomView({ startSec: 60, endSec: 120 }, 2, 90, 180);

    expect(view).toEqual({ startSec: 75, endSec: 105 });
    const ratio = (90 - view.startSec) / viewSpanSec(view);
    expect(ratio).toBeCloseTo(0.5, 6);
  });

  it("端に寄った時刻を固定して拡大しても、曲の範囲内に収まる", () => {
    const view = zoomView({ startSec: 0, endSec: 90 }, 2, 5, 180);

    expect(view.startSec).toBeGreaterThanOrEqual(0);
    expect(view.endSec).toBeLessThanOrEqual(180);
    // 固定した時刻は表示区間内に残る。
    expect(view.startSec).toBeLessThanOrEqual(5);
    expect(view.endSec).toBeGreaterThanOrEqual(5);
  });

  it("縮小は全体表示で止まる", () => {
    const view = zoomView({ startSec: 0, endSec: 180 }, 0.5, 90, 180);

    expect(view).toEqual({ startSec: 0, endSec: 180 });
  });

  it("横スクロールは端で止まる", () => {
    expect(panView({ startSec: 0, endSec: 10 }, -5, 100)).toEqual({ startSec: 0, endSec: 10 });
    expect(panView({ startSec: 90, endSec: 100 }, 5, 100)).toEqual({
      startSec: 90,
      endSec: 100,
    });
  });

  it("スクロールバーの位置と表示区間を往復できる", () => {
    const view = { startSec: 30, endSec: 150 };

    expect(panFraction(view, 180)).toBeCloseTo(0.5, 6);
    expect(viewFromFraction(0.5, 120, 180)).toEqual(view);
    expect(viewFromFraction(1, 120, 180)).toEqual({ startSec: 60, endSec: 180 });
    // 全体表示ではスクロールの余地が無い。
    expect(panFraction(fullView(180), 180)).toBe(0);
  });

  it("拡大したときだけ高解像度のピークを要求する", () => {
    // 全曲表示は既定解像度(バックエンドのキャッシュをそのまま使う)。
    expect(requestedBuckets(fullView(180), 180)).toBe(DEFAULT_PEAKS_BUCKETS);
    // 3分の曲で18秒ぶんを表示するなら、1点が数十msになるよう解像度を上げる。
    expect(requestedBuckets({ startSec: 0, endSec: 18 }, 180)).toBeGreaterThan(
      DEFAULT_PEAKS_BUCKETS,
    );
    // 深い拡大では上限で頭打ちになる(JSONを肥大化させない)。
    expect(requestedBuckets({ startSec: 0, endSec: 0.5 }, 3600)).toBe(MAX_REQUESTED_BUCKETS);
  });

  it("解像度は離散化され、ズームのたびに新しいキャッシュが増えない", () => {
    // 連続値をそのまま要求すると、ズーム操作ごとに `{name}.{buckets}.json`(削除は
    // 再分離時のみ)が増え続けるため、2の冪へ切り上げる。取り得る値は数個に収まる。
    const values = new Set<number>();
    for (let span = 180; span >= 0.5; span -= 0.25) {
      const buckets = requestedBuckets({ startSec: 0, endSec: span }, 180);
      values.add(buckets);
      const isPowerOfTwo = Math.log2(buckets) % 1 === 0;
      expect(
        isPowerOfTwo || buckets === DEFAULT_PEAKS_BUCKETS || buckets === MAX_REQUESTED_BUCKETS,
      ).toBe(true);
    }

    // 取り得る値: 1000(既定)、2048、4096、8192、16384、20000(上限)の6段階。
    expect(values.size).toBeLessThanOrEqual(6);
    expect(Math.min(...values)).toBe(DEFAULT_PEAKS_BUCKETS);
  });

  it("表示区間に含まれる点だけを切り出す", () => {
    // 100秒を1000点で持つなら1点=0.1秒。10〜12秒は100〜120点目。
    expect(visiblePointRange(1000, { startSec: 10, endSec: 12 }, 100)).toEqual({
      start: 100,
      end: 120,
    });
    // 全曲表示では全点。
    expect(visiblePointRange(5, fullView(100), 100)).toEqual({ start: 0, end: 5 });
  });

  it("点が無いときは粒度0・切り出し範囲0にする", () => {
    expect(pointDurationSec(0, 100)).toBe(0);
    expect(visiblePointRange(0, fullView(100), 100)).toEqual({ start: 0, end: 0 });
  });

  it("目盛りは表示幅に応じて間引かれ、位置は0〜1の比率で返る", () => {
    const ticks = rulerTicks({ startSec: 0, endSec: 180 }, 900);

    // 幅900px・最小間隔72pxなら最大12本 → 15秒刻み。
    expect(ticks.map((tick) => tick.label)).toEqual([
      "0",
      "15",
      "30",
      "45",
      "60",
      "75",
      "90",
      "105",
      "120",
      "135",
      "150",
      "165",
      "180",
    ]);
    expect(ticks[0].ratio).toBeCloseTo(0, 6);
    expect(ticks[ticks.length - 1].ratio).toBeCloseTo(1, 6);
  });

  it("拡大時は刻みを細かくし、小数の桁を増やす", () => {
    const ticks = rulerTicks({ startSec: 10, endSec: 11 }, 900);

    expect(ticks).toHaveLength(11);
    expect(ticks[0]).toMatchObject({ timeSec: 10, label: "10.0", ratio: 0 });
    expect(ticks[ticks.length - 1].label).toBe("11.0");
  });

  it("幅が0のときは目盛りを作らない", () => {
    expect(rulerTicks(fullView(180), 0)).toEqual([]);
    expect(rulerTicks({ startSec: 10, endSec: 10 }, 900)).toEqual([]);
  });
});
