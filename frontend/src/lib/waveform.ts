/** ピークデータ(#21)を描画用の矩形リストへ変換する純粋関数。 */

export interface PeakBar {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * `[min, max]` の配列(振幅は概ね -1..1)を、幅1・高さ2の座標系(y=0が振幅+1、y=2が振幅-1)
 * 上の矩形リストへ変換する。呼び出し側は `<svg viewBox="0 0 {peaks.length} 2">` を使う想定。
 *
 * 振幅差がほぼ0(無音)のバケットも視認できるよう、高さの最小値を設ける。
 */
const MIN_BAR_HEIGHT = 0.02;

export function peaksToBars(peaks: number[][]): PeakBar[] {
  return peaks.map(([min, max], i) => {
    const safeMax = max ?? 0;
    const safeMin = min ?? 0;
    const height = Math.max(safeMax - safeMin, MIN_BAR_HEIGHT);
    return { x: i, y: 1 - safeMax, width: 1, height };
  });
}
