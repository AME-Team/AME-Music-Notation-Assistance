import type { Beatmap } from "../api/client";

export interface BeatGridLine {
  timeSec: number;
  isDownbeat: boolean;
}

/**
 * beatmap(#18)から、波形上に描画するビート/ダウンビート線の一覧を計算する純粋関数。
 *
 * bar=0 のビート(先頭ダウンビートより前のピックアップ拍、backend/app/pipeline/beat.py
 * の `_assign_bars` 参照)は小節構造の対象外のため線を引かない。
 *
 * `downbeats_sec` の値はサーバ側で `beats[].time_sec` のいずれかと同一の浮動小数値
 * として書き出され(backend/app/pipeline/beat.py の `build_beatmap`)、JSON往復でも
 * 精度が保たれるため、ここでは許容誤差を設けず厳密一致で照合する。
 */
export function computeBeatGridLines(beatmap: Beatmap): BeatGridLine[] {
  const downbeats = new Set(beatmap.downbeats_sec);

  return beatmap.beats
    .filter((beat) => beat.bar > 0)
    .map((beat) => ({ timeSec: beat.time_sec, isDownbeat: downbeats.has(beat.time_sec) }));
}
