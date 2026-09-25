/**
 * 全作業ページに出す「先頭N小節プレビュー」の計算(#172)。
 *
 * 補正は**実物のMIDIと楽譜を見ながら**行う必要がある(このアプリの根幹)。そのため
 * どの作業ページでも、先頭N小節ぶんのMIDIバーと楽譜を常に見えるようにする。ここには
 * 「どの小節までを表示するか」「その範囲にどの音符が入るか」「MIDIバーのどこへ描くか」
 * という純粋な計算だけを置き、描画は`components/MidiBar.tsx`へ任せる。
 */

import type { ScoreIR } from "../api/client";
import { barBoundariesTicks } from "./pianoRoll";

/** 既定の表示小節数(ユーザー指定: 先頭4小節)。 */
export const DEFAULT_PREVIEW_BARS = 4;

/** 設定で選べる表示小節数。 */
export const PREVIEW_BARS_CHOICES = [1, 2, 4, 8, 16, 32];

/**
 * localStorage等から読んだ値を、選択肢のいずれかへ寄せる。
 *
 * 保存値はユーザーが直接書き換えられるため、そのまま使うと
 * `previewWindow`が壊れた範囲を返しうる(0小節など)。既定値へ落とす。
 */
export function normalizePreviewBars(value: unknown): number {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return DEFAULT_PREVIEW_BARS;
  const rounded = Math.floor(number);
  return PREVIEW_BARS_CHOICES.includes(rounded) ? rounded : DEFAULT_PREVIEW_BARS;
}

const PREVIEW_BARS_STORAGE_KEY = "ame.scoreView.bars";

/** 保存された表示小節数を読む(壊れていれば既定値)。 */
export function readStoredPreviewBars(storage: Storage): number {
  return normalizePreviewBars(storage.getItem(PREVIEW_BARS_STORAGE_KEY));
}

/** 表示小節数を保存する。 */
export function storePreviewBars(storage: Storage, bars: number): void {
  storage.setItem(PREVIEW_BARS_STORAGE_KEY, String(normalizePreviewBars(bars)));
}

/** プレビューが扱う範囲(小節とtick)。 */
export interface BarWindow {
  /** 表示する最後の小節番号(1始まり)。楽譜が短ければ実際の小節数に丸める。 */
  toBar: number;
  /** 先頭からのtick範囲。 */
  fromTick: number;
  toTick: number;
  /** 区切り(小節線)のtick。先頭は必ず0。 */
  boundaries: number[];
  divisions: number;
}

/** プレビューに出す音符(tick空間)。 */
export interface PreviewNote {
  /** スコア内で一意な音符id(描画のkeyに使う。同一音高・同一位置のユニゾンでも衝突しない)。 */
  id: number;
  midi: number;
  onsetTick: number;
  durationTick: number;
}

/** スコア中の最後のtick(`onset_tick`/`duration_tick`が揃う音符のみ数える)。 */
function lastTickOf(score: Pick<ScoreIR, "parts">): number {
  let last = 0;
  for (const part of score.parts) {
    for (const note of part.notes) {
      if (note.onset_tick == null || note.duration_tick == null) continue;
      last = Math.max(last, note.onset_tick + note.duration_tick);
    }
  }
  return last;
}

/** プレビューを作れない理由。呼び出し側が案内文を出し分けるために返す。 */
export type PreviewUnavailableReason = "no-notes" | "invalid-divisions" | "too-short";

type WindowResult = { window: BarWindow } | { reason: PreviewUnavailableReason };

function analyzeWindow(
  score: Pick<ScoreIR, "time_signatures" | "divisions" | "parts">,
  bars: number,
): WindowResult {
  const divisions = score.divisions;
  if (!Number.isFinite(divisions) || divisions <= 0) return { reason: "invalid-divisions" };

  // 量子化前は`onset_tick`が無く、tick空間の範囲を作れない。
  const lastTick = lastTickOf(score);
  if (lastTick <= 0) return { reason: "no-notes" };

  const boundaries = barBoundariesTicks(score.time_signatures, divisions, lastTick);
  if (boundaries.length < 2) return { reason: "too-short" };

  const toBar = Math.max(1, Math.min(Math.floor(bars), boundaries.length - 1));
  return {
    window: {
      toBar,
      fromTick: boundaries[0],
      toTick: boundaries[toBar],
      boundaries,
      divisions,
    },
  };
}

/**
 * スコアの先頭から`bars`小節ぶんの範囲を返す。作れない場合は`null`
 * (理由は`previewUnavailableReason`で取る)。
 */
export function previewWindow(
  score: Pick<ScoreIR, "time_signatures" | "divisions" | "parts">,
  bars: number,
): BarWindow | null {
  const result = analyzeWindow(score, bars);
  return "window" in result ? result.window : null;
}

/** 範囲を作れない理由(作れる場合は`null`)。 */
export function previewUnavailableReason(
  score: Pick<ScoreIR, "time_signatures" | "divisions" | "parts">,
  bars: number,
): PreviewUnavailableReason | null {
  const result = analyzeWindow(score, bars);
  return "reason" in result ? result.reason : null;
}

/** 範囲に入る音符を返す(範囲の外へはみ出す長さは切り詰める)。 */
export function previewNotes(score: Pick<ScoreIR, "parts">, window: BarWindow): PreviewNote[] {
  const notes: PreviewNote[] = [];
  for (const part of score.parts) {
    for (const note of part.notes) {
      if (note.onset_tick == null || note.duration_tick == null) continue;
      if (note.onset_tick < window.fromTick || note.onset_tick >= window.toTick) continue;
      const end = Math.min(note.onset_tick + note.duration_tick, window.toTick);
      notes.push({
        id: note.id,
        midi: note.midi,
        onsetTick: note.onset_tick,
        durationTick: Math.max(end - note.onset_tick, 1),
      });
    }
  }
  return notes.sort((a, b) => a.onsetTick - b.onsetTick || a.midi - b.midi);
}

/** 描画に使う音高の範囲(音符が無ければ`null`)。 */
export function previewPitchRange(
  notes: PreviewNote[],
): { minMidi: number; maxMidi: number } | null {
  if (notes.length === 0) return null;
  let minMidi = Number.POSITIVE_INFINITY;
  let maxMidi = Number.NEGATIVE_INFINITY;
  for (const note of notes) {
    minMidi = Math.min(minMidi, note.midi);
    maxMidi = Math.max(maxMidi, note.midi);
  }
  return { minMidi, maxMidi };
}

/** MIDIバー1音ぶんの矩形(px)。 */
export interface NoteRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 音符をMIDIバーの矩形へ変換する(X=tick、Y=MIDI。上ほど高音)。
 *
 * 最も短い音符でも見えるよう、幅と高さに下限を設ける(4小節ぶんを数百pxで見るため、
 * 32分音符は数pxになる)。
 */
export function midiBarRect(
  note: PreviewNote,
  window: BarWindow,
  range: { minMidi: number; maxMidi: number },
  size: { width: number; height: number },
): NoteRect {
  const span = Math.max(window.toTick - window.fromTick, 1);
  const pitches = Math.max(range.maxMidi - range.minMidi + 1, 1);
  const rowHeight = size.height / pitches;

  return {
    x: ((note.onsetTick - window.fromTick) / span) * size.width,
    y: ((range.maxMidi - note.midi) / pitches) * size.height,
    width: Math.max((note.durationTick / span) * size.width, 1),
    height: Math.max(rowHeight - 1, 1),
  };
}
