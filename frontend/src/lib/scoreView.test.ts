import { describe, expect, it } from "vitest";
import type { ScoreIR } from "../api/client";
import {
  DEFAULT_PREVIEW_BARS,
  midiBarRect,
  normalizePreviewBars,
  previewNotes,
  previewPitchRange,
  previewWindow,
  readStoredPreviewBars,
  storePreviewBars,
} from "./scoreView";

/** 4/4・divisions=480(1拍=480tick、1小節=1920tick)の最小スコア。 */
function makeScore(): ScoreIR {
  return {
    schema_version: 1,
    project_id: "proj",
    source: { filename: "a.wav", duration_sec: 10, sample_rate: 44100 },
    divisions: 480,
    tempo_map: [{ bar: 1, beat: 1, bpm: 120 }],
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    key_signatures: [],
    chords: [],
    parts: [
      {
        id: "piano",
        name: "Piano",
        notes: [
          { id: 1, midi: 60, onset_tick: 0, duration_tick: 480 },
          { id: 2, midi: 62, onset_tick: 480, duration_tick: 480 },
          // 2小節目
          { id: 3, midi: 64, onset_tick: 1920, duration_tick: 480 },
          // 3小節目
          { id: 4, midi: 65, onset_tick: 3840, duration_tick: 480 },
        ],
      },
    ],
    meta: { stages: {} },
    next_note_id: 5,
  } as unknown as ScoreIR;
}

describe("normalizePreviewBars", () => {
  it("選択肢にある値はそのまま、無い値は既定へ落とす", () => {
    expect(normalizePreviewBars(8)).toBe(8);
    expect(normalizePreviewBars("16")).toBe(16);
    expect(normalizePreviewBars(3)).toBe(DEFAULT_PREVIEW_BARS);
    expect(normalizePreviewBars(0)).toBe(DEFAULT_PREVIEW_BARS);
    expect(normalizePreviewBars("abc")).toBe(DEFAULT_PREVIEW_BARS);
    expect(normalizePreviewBars(null)).toBe(DEFAULT_PREVIEW_BARS);
  });

  it("保存値の読み書きが往復する(壊れた値は既定へ)", () => {
    const storage = new Map<string, string>();
    const fake = {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => void storage.set(key, value),
    } as unknown as Storage;

    storePreviewBars(fake, 32);
    expect(readStoredPreviewBars(fake)).toBe(32);

    storage.set("ame.scoreView.bars", "999");
    expect(readStoredPreviewBars(fake)).toBe(DEFAULT_PREVIEW_BARS);
  });
});

describe("previewWindow", () => {
  it("先頭N小節のtick範囲を返す(4/4・480divisions)", () => {
    const window = previewWindow(makeScore(), 2);
    expect(window).not.toBeNull();
    expect(window?.toBar).toBe(2);
    expect(window?.fromTick).toBe(0);
    expect(window?.toTick).toBe(3840);
  });

  it("曲より大きい小節数を指定しても、実在する小節までに丸める", () => {
    expect(previewWindow(makeScore(), 100)?.toBar).toBe(3);
  });

  it("量子化前(音符が無い/divisions不正)は null", () => {
    const score = makeScore();
    expect(previewWindow({ ...score, parts: [] }, 4)).toBeNull();
    expect(previewWindow({ ...score, divisions: 0 }, 4)).toBeNull();
  });
});

describe("previewNotes", () => {
  it("範囲内の音符だけを返し、はみ出す長さは切り詰める", () => {
    const score = makeScore();
    const window = previewWindow(score, 2);
    if (!window) throw new Error("window");

    const notes = previewNotes(score, window);
    expect(notes.map((note) => note.midi)).toEqual([60, 62, 64]);

    // 2小節目を跨ぐ音符は範囲の終わりで切る。
    const crossing = previewNotes(
      {
        parts: [{ notes: [{ id: 99, midi: 70, onset_tick: 1440, duration_tick: 3840 }] }],
      } as never,
      window,
    );
    expect(crossing[0].durationTick).toBe(window.toTick - 1440);
  });
});

describe("midiBarRect", () => {
  it("Xはtick、Yは音高に対応し、最小の幅・高さを保証する", () => {
    const score = makeScore();
    const window = previewWindow(score, 2);
    if (!window) throw new Error("window");
    const notes = previewNotes(score, window);
    const range = previewPitchRange(notes);
    if (!range) throw new Error("range");

    const size = { width: 1000, height: 100 };
    const first = midiBarRect(notes[0], window, range, size);
    // 先頭の音符は左端、長さ1拍ぶん。
    expect(first.x).toBe(0);
    expect(first.width).toBeCloseTo(125, 6);
    // 音高が高いほどYは小さい(上へ描く)。
    const rangeWithHigh = { minMidi: 60, maxMidi: 72 };
    const low = midiBarRect(
      { id: 1, midi: 60, onsetTick: 0, durationTick: 480 },
      window,
      rangeWithHigh,
      size,
    );
    const high = midiBarRect(
      { id: 2, midi: 72, onsetTick: 0, durationTick: 480 },
      window,
      rangeWithHigh,
      size,
    );
    expect(high.y).toBeLessThan(low.y);

    // 極端に短い音符でも幅1px・高さ1pxは確保する。
    const tiny = midiBarRect(
      { id: 1, midi: 60, onsetTick: 0, durationTick: 1 },
      window,
      { minMidi: 60, maxMidi: 84 },
      size,
    );
    expect(tiny.width).toBeGreaterThanOrEqual(1);
    expect(tiny.height).toBeGreaterThanOrEqual(1);
  });

  it("音符が無ければ音高範囲は null", () => {
    expect(previewPitchRange([])).toBeNull();
  });
});
