/**
 * TransportBar(#34)の座標変換・表示整形を担う純粋関数群。`pianoRoll.ts`と
 * 同じ方針(Tone.js配線・DOM描画から計算ロジックを剥がしてテスト可能にする)。
 */

export interface PlaybackNote {
  onset_sec: number;
  onset_tick: number | null;
}

export interface TempoMapEntry {
  bar: number;
  beat: number;
  bpm: number;
}

const DEFAULT_BPM = 120;

/**
 * 再生位置(秒)を、現在のscore内のノート点列`(onset_sec, onset_tick)`からの
 * 線形補間でtickへ変換する。`tempo_map`からのテンポ積分(`pipeline/quantize.py`の
 * `beat_tick_anchors`/`ticks_to_seconds`)をTS側に複製するより単純であり、この
 * 機能はUI表示(小節番号・プレイヘッド位置)専用でMIDI再生自体の音声的な正確さには
 * 影響しないため、この近似で十分と判断する(#34設計判断)。
 *
 * `onset_tick`が`null`(未量子化)のノートは点列から除外する。点列が空/1点のみ
 * なら安全にフォールバックする(空なら0、1点のみならその点のtickを返す)。
 */
export function secondsToTick(notes: readonly PlaybackNote[], seconds: number): number {
  const points = notes
    .filter((n): n is PlaybackNote & { onset_tick: number } => n.onset_tick != null)
    .map((n) => ({ sec: n.onset_sec, tick: n.onset_tick }))
    .sort((a, b) => a.sec - b.sec);

  if (points.length === 0) return 0;
  if (points.length === 1) return points[0].tick;

  if (seconds <= points[0].sec) {
    const [p0, p1] = points;
    return interpolateTick(p0, p1, seconds);
  }
  const last = points[points.length - 1];
  if (seconds >= last.sec) {
    const p0 = points[points.length - 2];
    return interpolateTick(p0, last, seconds);
  }

  let lo = 0;
  let hi = points.length - 1;
  while (lo + 1 < hi) {
    const mid = (lo + hi) >>> 1;
    if (points[mid].sec <= seconds) lo = mid;
    else hi = mid;
  }
  return interpolateTick(points[lo], points[hi], seconds);
}

function interpolateTick(
  p0: { sec: number; tick: number },
  p1: { sec: number; tick: number },
  seconds: number,
): number {
  if (p1.sec === p0.sec) return p0.tick;
  const fraction = (seconds - p0.sec) / (p1.sec - p0.sec);
  return p0.tick + fraction * (p1.tick - p0.tick);
}

/**
 * `bar`時点で有効なBPMを、`tempo_map`の直近以前のエントリから前方補完で求める
 * (`pianoRoll.ts`の`timeSignatureAtBar`と同型)。エントリが無ければ既定120を返す。
 */
export function bpmAtBar(tempoMap: readonly TempoMapEntry[], bar: number): number {
  let bpm = DEFAULT_BPM;
  for (const entry of tempoMap) {
    if (entry.bar <= bar) {
      bpm = entry.bpm;
    } else {
      break;
    }
  }
  return bpm;
}

/** 秒数を`mm:ss.d`形式に整形する(TransportBarの時間表示用)。負数・NaNは0扱い。 */
export function formatTime(seconds: number): string {
  const safe = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  // 小数の減算を繰り返すと浮動小数点誤差で末尾の桁が1つずれうる(例: 32.4 - 32
  // = 32.3999...)ため、先に整数の「0.1秒単位」へ丸めてから分/秒/小数第1位を
  // 導出する。
  const totalTenths = Math.round(safe * 10);
  const minutes = Math.floor(totalTenths / 600);
  const wholeSeconds = Math.floor((totalTenths % 600) / 10);
  const tenths = totalTenths % 10;
  return `${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}.${tenths}`;
}
