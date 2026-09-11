import { describe, expect, it } from "vitest";
import { bpmAtBar, formatTime, secondsToTick } from "./transport";

describe("secondsToTick", () => {
  it("returns 0 for an empty note list", () => {
    expect(secondsToTick([], 1.5)).toBe(0);
  });

  it("returns the single point's tick regardless of the requested seconds", () => {
    const notes = [{ onset_sec: 1, onset_tick: 480 }];
    expect(secondsToTick(notes, 0)).toBe(480);
    expect(secondsToTick(notes, 100)).toBe(480);
  });

  it("interpolates linearly between two points", () => {
    const notes = [
      { onset_sec: 0, onset_tick: 0 },
      { onset_sec: 2, onset_tick: 1920 },
    ];
    expect(secondsToTick(notes, 1)).toBeCloseTo(960);
  });

  it("clamps below the first point using the first segment's slope", () => {
    const notes = [
      { onset_sec: 1, onset_tick: 480 },
      { onset_sec: 2, onset_tick: 960 },
    ];
    expect(secondsToTick(notes, 0)).toBeCloseTo(0);
  });

  it("clamps beyond the last point using the last segment's slope", () => {
    const notes = [
      { onset_sec: 0, onset_tick: 0 },
      { onset_sec: 1, onset_tick: 480 },
    ];
    expect(secondsToTick(notes, 2)).toBeCloseTo(960);
  });

  it("excludes notes with a null onset_tick (unquantized)", () => {
    const notes = [
      { onset_sec: 0, onset_tick: 0 },
      { onset_sec: 1, onset_tick: null },
      { onset_sec: 2, onset_tick: 1920 },
    ];
    expect(secondsToTick(notes, 1)).toBeCloseTo(960);
  });

  it("binary-searches the correct segment among many points", () => {
    const notes = Array.from({ length: 10 }, (_, i) => ({
      onset_sec: i,
      onset_tick: i * 480,
    }));
    expect(secondsToTick(notes, 5.5)).toBeCloseTo(2640);
  });
});

describe("bpmAtBar", () => {
  it("defaults to 120 when the tempo map is empty", () => {
    expect(bpmAtBar([], 3)).toBe(120);
  });

  it("forward-fills from the most recent change", () => {
    const tempoMap = [
      { bar: 1, beat: 0, bpm: 100 },
      { bar: 5, beat: 0, bpm: 140 },
    ];
    expect(bpmAtBar(tempoMap, 1)).toBe(100);
    expect(bpmAtBar(tempoMap, 4)).toBe(100);
    expect(bpmAtBar(tempoMap, 5)).toBe(140);
    expect(bpmAtBar(tempoMap, 100)).toBe(140);
  });

  it("defaults to 120 before the first entry", () => {
    const tempoMap = [{ bar: 3, beat: 0, bpm: 90 }];
    expect(bpmAtBar(tempoMap, 1)).toBe(120);
  });
});

describe("formatTime", () => {
  it("formats zero", () => {
    expect(formatTime(0)).toBe("00:00.0");
  });

  it("formats sub-minute values with a tenths digit", () => {
    expect(formatTime(32.4)).toBe("00:32.4");
  });

  it("formats values over a minute", () => {
    expect(formatTime(187.0)).toBe("03:07.0");
  });

  it("treats negative/NaN as zero (defensive)", () => {
    expect(formatTime(-5)).toBe("00:00.0");
    expect(formatTime(Number.NaN)).toBe("00:00.0");
  });
});
