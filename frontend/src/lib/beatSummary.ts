import type { Beatmap } from "../api/client";

/**
 * #167: 「②テンポ・拍の検出」画面で、検出結果(`beatmap.json`)を
 * 人間が読める形に集計する純関数群。
 *
 * 推定結果そのもの(代表テンポ・拍子・拍の規模)を表示する場所が無く、
 * 画面には補正フォーム(`BeatGridEditor`)しか出ていなかった。
 *
 * なお`tempo_map`は**ビート位置ごとの瞬間BPM**(`backend/app/pipeline/beat.py`
 * の`_tempo_map`: 直前の拍との間隔から算出)であり、曲全体の代表テンポではない。
 * そのため代表値は中央値とし、幅(最小〜最大)も併せて返す。
 */

/** 拍一覧の1行。 */
export interface BeatTableRow {
  /** 1始まりの通し番号(全拍)。 */
  index: number;
  /** 小節番号。0は先頭ダウンビートより前のピックアップ拍。 */
  bar: number;
  /** 小節内の拍番号(1始まり)。ピックアップ拍は0。 */
  beatInBar: number;
  timeSec: number;
  /** `tempo_map`にある瞬間BPM(先頭の拍・ピックアップ拍はnull)。 */
  bpm: number | null;
}

/** 支配的な拍子と異なる拍子が現れる区間。 */
export interface TimeSignatureChange {
  /** 区間の開始小節。 */
  bar: number;
  /** 区間の終了小節(この拍子が続く最後の小節)。 */
  endBar: number;
  numerator: number;
  denominator: number;
}

export interface BeatSummary {
  beatCount: number;
  /** 先頭ダウンビートより前の拍(bar=0)の数。 */
  pickupBeatCount: number;
  /** 拍が存在する小節の数(重複なし)。 */
  barCount: number;
  downbeatCount: number;
  firstBeatSec: number | null;
  lastBeatSec: number | null;
  /** 代表テンポ(瞬間BPMの中央値)。 */
  tempoBpm: number | null;
  tempoMinBpm: number | null;
  tempoMaxBpm: number | null;
  /** テンポの幅が`TEMPO_STABLE_TOLERANCE_BPM`以内で「ほぼ一定」と言えるか。 */
  tempoIsStable: boolean;
  /** 平均拍間隔(秒)。 */
  beatIntervalSec: number | null;
  /** 支配的な拍子(例 "4/4")。拍子情報が無ければnull。 */
  timeSignature: string | null;
  /** 支配的な拍子と異なる拍子の小節(小節番号順)。 */
  timeSignatureChanges: TimeSignatureChange[];
  /** 信頼度(`Beatmap.confidence`は0〜1)を0〜100の整数にしたもの。 */
  confidencePercent: number;
  rows: BeatTableRow[];
}

/** テンポの幅がこの値(BPM)以内なら「ほぼ一定」と見なす(実測の揺れ±2BPM程度を許容)。 */
export const TEMPO_STABLE_TOLERANCE_BPM = 4;

/** 拍一覧の初期表示行数。これを超える場合は「すべて表示」で展開する。 */
export const MAX_VISIBLE_ROWS = 12;

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function signatureLabel(entry: { numerator: number; denominator: number }): string {
  return `${entry.numerator}/${entry.denominator}`;
}

/**
 * 支配的な拍子を「その拍子で書かれている小節数」で選ぶ。エントリは拍子が変わる
 * 小節にのみ現れるため、各エントリの担当小節数は「次のエントリの小節 - 自分の小節」
 * (最後のエントリは末尾まで)で求まる。同じ拍子が複数区間に現れる場合は合算する。
 *
 * 単純に「最初のエントリ」を採ると、実データのように一部の小節だけ別拍子と判定された
 * 場合(実測: 4/4が15小節・2/4が2小節)でも先頭の拍子しか見えなくなる。
 */
function dominantSignature(
  entries: { bar: number; numerator: number; denominator: number; label: string }[],
  lastBar: number,
): string | null {
  if (entries.length === 0) return null;

  const coverage = new Map<string, { bars: number; firstBar: number }>();
  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    const next = entries[i + 1];
    const span = Math.max(1, (next ? next.bar : lastBar + 1) - entry.bar);
    const current = coverage.get(entry.label);
    if (current) {
      current.bars += span;
    } else {
      coverage.set(entry.label, { bars: span, firstBar: entry.bar });
    }
  }

  let best: { label: string; bars: number; firstBar: number } | null = null;
  for (const [label, value] of coverage) {
    if (
      best === null ||
      value.bars > best.bars ||
      (value.bars === best.bars && value.firstBar < best.firstBar)
    ) {
      best = { label, bars: value.bars, firstBar: value.firstBar };
    }
  }
  return best?.label ?? null;
}

/** `beatmap.json`を画面表示用に集計する。空データでも例外を出さず、null/0を返す。 */
export function summarizeBeatmap(beatmap: Beatmap): BeatSummary {
  const beats = beatmap.beats;
  const tempoMap = beatmap.tempo_map;
  const timeSignatures = beatmap.time_signatures;

  const rows: BeatTableRow[] = beats.map((beat, index) => ({
    index: index + 1,
    bar: beat.bar,
    beatInBar: beat.beat_in_bar,
    timeSec: beat.time_sec,
    bpm: null,
  }));

  // 瞬間BPMはビート位置(小節・小節内の拍)で引く。`tempo_map`は先頭の拍と
  // ピックアップ拍を含まないため、位置が一致しない拍はnullのままにする。
  const bpmByPosition = new Map<string, number>();
  for (const entry of tempoMap) {
    bpmByPosition.set(`${entry.bar}:${entry.beat}`, entry.bpm);
  }
  for (const row of rows) {
    row.bpm = bpmByPosition.get(`${row.bar}:${row.beatInBar}`) ?? null;
  }

  const bpms = tempoMap.map((entry) => entry.bpm);
  const tempoBpm = median(bpms);
  const tempoMinBpm = bpms.length > 0 ? Math.min(...bpms) : null;
  const tempoMaxBpm = bpms.length > 0 ? Math.max(...bpms) : null;

  const bars = beats.filter((beat) => beat.bar > 0).map((beat) => beat.bar);
  const distinctBars = [...new Set(bars)];
  const lastBar = distinctBars.length > 0 ? Math.max(...distinctBars) : 0;

  const sortedSignatures = [...timeSignatures]
    .sort((a, b) => a.bar - b.bar)
    .map((entry) => ({ ...entry, label: signatureLabel(entry) }));
  const timeSignature = dominantSignature(sortedSignatures, lastBar);
  // 各エントリの拍子は「次のエントリの小節の1つ前」まで続き、最後のエントリは末尾まで
  // 続く。開始小節だけを表示すると、複数小節にまたがる変拍子を「その小節だけ」と
  // 誤読させるため、区間(endBar)として持つ(レビュー指摘)。
  const timeSignatureChanges: TimeSignatureChange[] = sortedSignatures
    .map((entry, index) => {
      const next = sortedSignatures[index + 1];
      return {
        bar: entry.bar,
        endBar: Math.max(entry.bar, next ? next.bar - 1 : lastBar),
        numerator: entry.numerator,
        denominator: entry.denominator,
        label: entry.label,
      };
    })
    .filter((entry) => entry.label !== timeSignature)
    .map(({ bar, endBar, numerator, denominator }) => ({ bar, endBar, numerator, denominator }));

  const firstBeatSec = beats.length > 0 ? beats[0].time_sec : null;
  const lastBeatSec = beats.length > 0 ? beats[beats.length - 1].time_sec : null;
  const beatIntervalSec =
    beats.length >= 2 && firstBeatSec !== null && lastBeatSec !== null
      ? (lastBeatSec - firstBeatSec) / (beats.length - 1)
      : null;

  const confidence = Number.isFinite(beatmap.confidence) ? beatmap.confidence : 0;

  return {
    beatCount: beats.length,
    pickupBeatCount: beats.filter((beat) => beat.bar === 0).length,
    barCount: distinctBars.length,
    downbeatCount: beatmap.downbeats_sec.length,
    firstBeatSec,
    lastBeatSec,
    tempoBpm,
    tempoMinBpm,
    tempoMaxBpm,
    tempoIsStable:
      tempoMinBpm !== null &&
      tempoMaxBpm !== null &&
      tempoMaxBpm - tempoMinBpm <= TEMPO_STABLE_TOLERANCE_BPM,
    beatIntervalSec,
    timeSignature,
    timeSignatureChanges,
    confidencePercent: Math.round(Math.min(1, Math.max(0, confidence)) * 100),
    rows,
  };
}

/** BPMを小数1桁で整形する(値が無ければ「—」)。 */
export function formatBpm(value: number | null): string {
  return value === null ? "—" : value.toFixed(1);
}

/** 秒を小数2桁で整形する(値が無ければ「—」)。 */
export function formatSeconds(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(2)} 秒`;
}

/** テンポの幅を表す補足文。ほぼ一定なら「ほぼ一定」、動く場合は幅を示す。 */
export function formatTempoNote(summary: BeatSummary): string {
  if (summary.tempoBpm === null || summary.tempoMinBpm === null || summary.tempoMaxBpm === null) {
    return "テンポを推定できませんでした";
  }
  if (summary.tempoIsStable) {
    return "ほぼ一定";
  }
  return `曲中で変動(${formatBpm(summary.tempoMinBpm)}〜${formatBpm(summary.tempoMaxBpm)} BPM)`;
}

/**
 * 支配的な拍子と異なる拍子の**区間**を表す補足文(無ければnull)。
 * 1小節だけの区間は「小節 10」、複数小節にまたがる場合は「小節 10〜14」と表記する。
 */
export function formatTimeSignatureChanges(summary: BeatSummary): string | null {
  if (summary.timeSignatureChanges.length === 0) return null;

  const bySignature = new Map<string, string[]>();
  for (const change of summary.timeSignatureChanges) {
    const label = `${change.numerator}/${change.denominator}`;
    const range = change.endBar > change.bar ? `${change.bar}〜${change.endBar}` : `${change.bar}`;
    const ranges = bySignature.get(label) ?? [];
    ranges.push(range);
    bySignature.set(label, ranges);
  }

  return [...bySignature]
    .map(([label, ranges]) => `小節 ${ranges.join("・")} は ${label}`)
    .join("、");
}

/** 拍の位置を「小節.拍」で表す(ピックアップ拍は「—」)。 */
export function formatBeatPosition(row: BeatTableRow): string {
  return row.bar > 0 && row.beatInBar > 0 ? `${row.bar}.${row.beatInBar}` : "—";
}
