import { describe, expect, it } from "vitest";
import {
  barBoundariesTicks,
  barNumberForTick,
  hitTestNote,
  midiToY,
  type PianoRollNote,
  tickToX,
  timeSignatureAtBar,
  visibleNoteRange,
  xToTick,
  yToMidi,
} from "./pianoRoll";

const NOTE = (overrides: Partial<PianoRollNote> = {}): PianoRollNote => ({
  id: 1,
  onset_tick: 0,
  duration_tick: 480,
  midi: 60,
  status: "active",
  ...overrides,
});

describe("timeSignatureAtBar", () => {
  it("defaults to 4/4 when no signatures are given", () => {
    expect(timeSignatureAtBar([], 5)).toEqual([4, 4]);
  });

  it("forward-fills from the most recent change", () => {
    const signatures = [
      { bar: 1, numerator: 4, denominator: 4 },
      { bar: 3, numerator: 6, denominator: 8 },
    ];
    expect(timeSignatureAtBar(signatures, 1)).toEqual([4, 4]);
    expect(timeSignatureAtBar(signatures, 2)).toEqual([4, 4]);
    expect(timeSignatureAtBar(signatures, 3)).toEqual([6, 8]);
    expect(timeSignatureAtBar(signatures, 10)).toEqual([6, 8]);
  });
});

describe("barBoundariesTicks", () => {
  it("accumulates by divisions*4 per bar under 4/4", () => {
    const boundaries = barBoundariesTicks([{ bar: 1, numerator: 4, denominator: 4 }], 480, 2000);
    expect(boundaries).toEqual([0, 1920, 3840]);
  });

  it("reflects a time signature change mid-way", () => {
    const signatures = [
      { bar: 1, numerator: 4, denominator: 4 },
      { bar: 2, numerator: 6, denominator: 8 },
    ];
    const boundaries = barBoundariesTicks(signatures, 480, 1920 + 1);
    // bar1(4/4)=1920tick、bar2(6/8)=6*(480*4)/8=1440tick。
    expect(boundaries).toEqual([0, 1920, 1920 + 1440]);
  });

  it("does not loop forever for a degenerate zero-numerator signature", () => {
    const boundaries = barBoundariesTicks([{ bar: 1, numerator: 0, denominator: 4 }], 480, 1000);
    expect(boundaries).toEqual([0]);
  });
});

describe("barNumberForTick", () => {
  const boundaries = [0, 1920, 3840, 5760]; // 4/4, divisions=480 -> 1920tick/bar

  it("returns 1 for the first tick of bar 1", () => {
    expect(barNumberForTick(boundaries, 0)).toBe(1);
  });

  it("returns the bar containing a mid-bar tick", () => {
    expect(barNumberForTick(boundaries, 1000)).toBe(1);
    expect(barNumberForTick(boundaries, 1920)).toBe(2);
    expect(barNumberForTick(boundaries, 2000)).toBe(2);
  });

  it("returns the last bar for a tick beyond the last boundary", () => {
    expect(barNumberForTick(boundaries, 999_999)).toBe(4);
  });

  it("clamps to bar 1 for a negative tick (defensive, should not occur in practice)", () => {
    expect(barNumberForTick(boundaries, -100)).toBe(1);
  });
});

describe("tick/pixel and midi/pixel conversions", () => {
  it("tickToX and xToTick are inverses", () => {
    const x = tickToX(1000, 200, 0.5);
    expect(xToTick(x, 200, 0.5)).toBeCloseTo(1000);
  });

  it("midiToY places the top-visible midi at y=0 and lower pitches further down", () => {
    expect(midiToY(72, 72, 10)).toBe(0);
    expect(midiToY(60, 72, 10)).toBe(120);
  });

  it("midiToY and yToMidi are inverses", () => {
    const y = midiToY(64, 80, 12);
    expect(yToMidi(y, 80, 12)).toBeCloseTo(64);
  });
});

describe("visibleNoteRange", () => {
  it("returns the full range when everything is visible", () => {
    const notes = [NOTE({ onset_tick: 0 }), NOTE({ onset_tick: 480 }), NOTE({ onset_tick: 960 })];
    expect(visibleNoteRange(notes, 0, 2000)).toEqual([0, 3]);
  });

  it("excludes notes starting well after the visible window", () => {
    const notes = [NOTE({ onset_tick: 0 }), NOTE({ onset_tick: 100_000 })];
    const [, end] = visibleNoteRange(notes, 0, 480);
    expect(end).toBe(1);
  });

  it("includes a long note that starts before the window but is within the lookback margin", () => {
    const notes = [NOTE({ onset_tick: 0, duration_tick: 10_000 })];
    const [start] = visibleNoteRange(notes, 5_000, 6_000);
    expect(start).toBe(0);
  });
});

describe("hitTestNote", () => {
  it("finds a note under the cursor and reports the move region for its body", () => {
    const notes = [NOTE({ id: 1, onset_tick: 0, duration_tick: 480, midi: 60 })];
    const hit = hitTestNote(notes, 100, 60, 1);
    expect(hit).not.toBeNull();
    expect(hit?.note.id).toBe(1);
    expect(hit?.region).toBe("move");
  });

  it("reports resize-right near the note's trailing edge", () => {
    const notes = [NOTE({ id: 1, onset_tick: 0, duration_tick: 480, midi: 60 })];
    const hit = hitTestNote(notes, 478, 60, 1);
    expect(hit?.region).toBe("resize-right");
  });

  it("returns null when no note matches the pitch", () => {
    const notes = [NOTE({ onset_tick: 0, duration_tick: 480, midi: 60 })];
    expect(hitTestNote(notes, 100, 61, 1)).toBeNull();
  });

  it("ignores deleted notes", () => {
    const notes = [NOTE({ onset_tick: 0, duration_tick: 480, midi: 60, status: "deleted" })];
    expect(hitTestNote(notes, 100, 60, 1)).toBeNull();
  });

  it("still allows moving a very short note at low zoom (regression)", () => {
    // #30-M3レビュー指摘: handleWidthをノート幅でクランプしないと、短い
    // ノート/低ズームでは判定が常にresize-rightになり移動できなくなる。
    const notes = [NOTE({ id: 1, onset_tick: 0, duration_tick: 10, midi: 60 })];
    const hit = hitTestNote(notes, 1, 60, 0.1); // pxPerTick=0.1 -> 素朴な計算では80tick分のハンドル幅
    expect(hit?.region).toBe("move");
  });

  it("treats a non-positive pxPerTick as no resize handle rather than Infinity/NaN", () => {
    const notes = [NOTE({ id: 1, onset_tick: 0, duration_tick: 480, midi: 60 })];
    const hit = hitTestNote(notes, 479, 60, 0);
    expect(hit?.region).toBe("move");
  });

  it("prefers the last (topmost drawn) note when multiple overlap", () => {
    const notes = [
      NOTE({ id: 1, onset_tick: 0, duration_tick: 480, midi: 60 }),
      NOTE({ id: 2, onset_tick: 0, duration_tick: 480, midi: 60 }),
    ];
    expect(hitTestNote(notes, 100, 60, 1)?.note.id).toBe(2);
  });
});
