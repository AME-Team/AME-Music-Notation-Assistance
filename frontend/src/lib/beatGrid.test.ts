import { describe, expect, it } from "vitest";
import type { Beatmap } from "../api/client";
import { computeBeatGridLines } from "./beatGrid";

function beatmap(overrides: Partial<Beatmap> = {}): Beatmap {
  return {
    beats: [],
    downbeats_sec: [],
    time_signatures: [],
    tempo_map: [],
    confidence: 0.9,
    source: "auto",
    ...overrides,
  };
}

describe("computeBeatGridLines", () => {
  it("marks beats whose time matches a downbeat as isDownbeat", () => {
    const lines = computeBeatGridLines(
      beatmap({
        beats: [
          { time_sec: 0.0, beat_in_bar: 1, bar: 1 },
          { time_sec: 0.5, beat_in_bar: 2, bar: 1 },
          { time_sec: 1.0, beat_in_bar: 3, bar: 1 },
          { time_sec: 1.5, beat_in_bar: 4, bar: 1 },
          { time_sec: 2.0, beat_in_bar: 1, bar: 2 },
        ],
        downbeats_sec: [0.0, 2.0],
      }),
    );

    expect(lines).toEqual([
      { timeSec: 0.0, isDownbeat: true },
      { timeSec: 0.5, isDownbeat: false },
      { timeSec: 1.0, isDownbeat: false },
      { timeSec: 1.5, isDownbeat: false },
      { timeSec: 2.0, isDownbeat: true },
    ]);
  });

  it("excludes pickup beats (bar=0) from the grid", () => {
    const lines = computeBeatGridLines(
      beatmap({
        beats: [
          { time_sec: -0.5, beat_in_bar: 0, bar: 0 },
          { time_sec: 0.0, beat_in_bar: 1, bar: 1 },
        ],
        downbeats_sec: [0.0],
      }),
    );

    expect(lines).toEqual([{ timeSec: 0.0, isDownbeat: true }]);
  });

  it("returns an empty list for an empty beatmap", () => {
    expect(computeBeatGridLines(beatmap())).toEqual([]);
  });
});
