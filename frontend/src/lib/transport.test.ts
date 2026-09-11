import { describe, expect, it } from "vitest";
import { bpmAtBar, buildTickMappingPoints, formatTime, secondsToTick } from "./transport";

describe("buildTickMappingPoints", () => {
  it("excludes notes with a null onset_tick (unquantized)", () => {
    const notes = [
      { onset_sec: 0, onset_tick: 0 },
      { onset_sec: 1, onset_tick: null },
      { onset_sec: 2, onset_tick: 1920 },
    ];
    expect(buildTickMappingPoints(notes)).toEqual([
      { sec: 0, tick: 0 },
      { sec: 2, tick: 1920 },
    ]);
  });

  it("sorts by onset_sec ascending regardless of input order", () => {
    const notes = [
      { onset_sec: 2, onset_tick: 1920 },
      { onset_sec: 0, onset_tick: 0 },
      { onset_sec: 1, onset_tick: 960 },
    ];
    expect(buildTickMappingPoints(notes)).toEqual([
      { sec: 0, tick: 0 },
      { sec: 1, tick: 960 },
      { sec: 2, tick: 1920 },
    ]);
  });
});

describe("secondsToTick", () => {
  it("returns 0 for an empty point list", () => {
    expect(secondsToTick([], 1.5)).toBe(0);
  });

  it("returns the single point's tick regardless of the requested seconds", () => {
    const points = [{ sec: 1, tick: 480 }];
    expect(secondsToTick(points, 0)).toBe(480);
    expect(secondsToTick(points, 100)).toBe(480);
  });

  it("interpolates linearly between two points", () => {
    const points = [
      { sec: 0, tick: 0 },
      { sec: 2, tick: 1920 },
    ];
    expect(secondsToTick(points, 1)).toBeCloseTo(960);
  });

  it("clamps below the first point using the first segment's slope", () => {
    const points = [
      { sec: 1, tick: 480 },
      { sec: 2, tick: 960 },
    ];
    expect(secondsToTick(points, 0)).toBeCloseTo(0);
  });

  it("clamps beyond the last point using the last segment's slope", () => {
    const points = [
      { sec: 0, tick: 0 },
      { sec: 1, tick: 480 },
    ];
    expect(secondsToTick(points, 2)).toBeCloseTo(960);
  });

  it("binary-searches the correct segment among many points", () => {
    const points = Array.from({ length: 10 }, (_, i) => ({
      sec: i,
      tick: i * 480,
    }));
    expect(secondsToTick(points, 5.5)).toBeCloseTo(2640);
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
