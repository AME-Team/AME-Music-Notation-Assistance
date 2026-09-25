/**
 * ビートグリッドの再生支援(クリック音のスケジューリング)の純粋な部分(#171)。
 *
 * 「楽曲にビートを挿入して、設定値が合っているか確認できる」ようにするため、
 * 再生中の音声位置に対して**次に鳴らす拍**を素早く引く必要がある。再生位置は
 * 数十ms先までを先読みして予約する方式にするため、ここでは「ある時刻以降で最初の拍」
 * を返す二分探索だけを提供する(音そのものは`BeatWaveformViewer`がWeb Audioで生成する)。
 */

/**
 * `timeSec`以降で最初の拍の添字(`beatTimes`は昇順)。見つからなければ`beatTimes.length`。
 *
 * 拍は数百件になるため線形探索ではなく二分探索にする(先読みループは25msごとに走る)。
 */
export function beatIndexAtOrAfter(beatTimes: number[], timeSec: number): number {
  let low = 0;
  let high = beatTimes.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (beatTimes[mid] < timeSec) low = mid + 1;
    else high = mid;
  }
  return low;
}

/** クリック音の周波数(Hz)。小節の先頭(ダウンビート)は高くして拍の頭が分かるようにする。 */
export const CLICK_FREQ_HZ = { downbeat: 1600, beat: 1000 } as const;

/** 小節の先頭かどうかでクリック音の周波数を選ぶ。 */
export function clickFrequencyHz(isDownbeat: boolean): number {
  return isDownbeat ? CLICK_FREQ_HZ.downbeat : CLICK_FREQ_HZ.beat;
}

/** クリック音の長さ(秒)。 */
export const CLICK_DURATION_SEC = 0.05;

/** クリック音の既定音量(0..1)。 */
export const DEFAULT_CLICK_VOLUME = 0.7;

/** 先読みして予約する幅(秒)。これを超えて先を予約すると停止時に鳴り残る。 */
export const CLICK_LOOKAHEAD_SEC = 0.2;

/**
 * ダウンビートと拍の時刻を突き合わせる許容誤差(秒)。
 *
 * `downbeats_sec`と`beats[].time_sec`は同じ推定結果から作られるが、保存や編集の
 * 経路が異なるため、丸めの結果が完全に一致する保証はない。厳密一致(`Set.has`)に
 * 依存すると、わずかな差で**全てのクリックが低い方の周波数になり**、「小節の頭は
 * 高く」という仕様が無言で壊れる(#173レビュー指摘)。5msは最速の曲でも隣の拍と
 * 間違えない十分に小さな値である。
 */
export const DOWNBEAT_MATCH_TOLERANCE_SEC = 0.005;

/**
 * 「ダウンビートである拍」の時刻の集合を作る。
 *
 * 返すのは`downbeats`ではなく**`beatTimes`側の値**にする。呼び出し側(クリック音の
 * 予約)は拍の時刻で引くため、キーの空間を揃えないと結局一致しない。
 *
 * `beat_in_bar == 1`で判定しないのは、`build_beatmap`の`_assign_bars`が
 * 「ダウンビートが1つも無いときは先頭ビートを暗黙の小節頭にする」仕様を持ち、
 * 先頭拍が常に`beat_in_bar == 1`になりうるためである
 * (`backend/app/pipeline/beatmap_edit.py::rotate_downbeat`のdocstring参照)。
 */
export function downbeatTimeSet(
  beatTimes: number[],
  downbeats: number[],
  toleranceSec: number = DOWNBEAT_MATCH_TOLERANCE_SEC,
): Set<number> {
  const result = new Set<number>();
  for (const downbeat of downbeats) {
    const index = beatIndexAtOrAfter(beatTimes, downbeat - toleranceSec);
    if (index >= beatTimes.length) continue;
    const candidate = beatTimes[index];
    if (Math.abs(candidate - downbeat) <= toleranceSec) result.add(candidate);
  }
  return result;
}

/**
 * 音声が`fromSec`から`toSec`へ進む間に鳴らす拍を返す(`timeSec`が範囲内の拍のみ)。
 * 先読み予約に使う。
 */
export function beatsInRange(beatTimes: number[], fromSec: number, toSec: number): number[] {
  const start = beatIndexAtOrAfter(beatTimes, fromSec);
  const result: number[] = [];
  for (let index = start; index < beatTimes.length; index += 1) {
    const timeSec = beatTimes[index];
    if (timeSec > toSec) break;
    result.push(timeSec);
  }
  return result;
}
