import { describe, expect, it } from "vitest";
import type { Beatmap } from "../api/client";
import {
  bpmOverrideIntervalSec,
  currentBeatIntervalSec,
  currentMeterLabel,
  offsetPreview,
} from "./beatEditPreview";
import { summarizeBeatmap } from "./beatSummary";

function makeBeatmap(overrides: Partial<Beatmap> = {}): Beatmap {
  return {
    beats: [
      { time_sec: 0.5, beat_in_bar: 1, bar: 1 },
      { time_sec: 1, beat_in_bar: 2, bar: 1 },
      { time_sec: 1.5, beat_in_bar: 3, bar: 1 },
      { time_sec: 2, beat_in_bar: 4, bar: 1 },
    ],
    downbeats_sec: [0.5],
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    // `tempo_map`は先頭の拍を含まない。
    tempo_map: [
      { bar: 1, beat: 2, bpm: 120 },
      { bar: 1, beat: 3, bpm: 120 },
      { bar: 1, beat: 4, bpm: 120 },
    ],
    confidence: 0.9,
    source: "auto",
    ...overrides,
  } as Beatmap;
}

describe("beatEditPreview", () => {
  it("全体オフセットの適用後は先頭と最後の拍が同じだけ動く", () => {
    const summary = summarizeBeatmap(makeBeatmap());

    expect(offsetPreview(summary, 0.25)).toEqual({ firstBeatSec: 0.75, lastBeatSec: 2.25 });
    expect(offsetPreview(summary, -0.5)).toEqual({ firstBeatSec: 0, lastBeatSec: 1.5 });
  });

  it("拍が無い場合や数値でない場合はプレビューしない", () => {
    const empty = summarizeBeatmap(makeBeatmap({ beats: [], downbeats_sec: [], tempo_map: [] }));

    expect(offsetPreview(empty, 0.1)).toBeNull();
    expect(offsetPreview(summarizeBeatmap(makeBeatmap()), Number.NaN)).toBeNull();
  });

  it("固定BPMから拍間隔を求め、0以下は受け付けない", () => {
    expect(bpmOverrideIntervalSec(120)).toBeCloseTo(0.5, 6);
    expect(bpmOverrideIntervalSec(90)).toBeCloseTo(2 / 3, 6);
    expect(bpmOverrideIntervalSec(0)).toBeNull();
    expect(bpmOverrideIntervalSec(-120)).toBeNull();
    expect(bpmOverrideIntervalSec(Number.NaN)).toBeNull();
  });

  it("現在のテンポからも拍間隔を求める", () => {
    const summary = summarizeBeatmap(makeBeatmap());

    expect(currentBeatIntervalSec(summary)).toBeCloseTo(0.5, 6);
    expect(currentBeatIntervalSec(summarizeBeatmap(makeBeatmap({ tempo_map: [] })))).toBeNull();
  });

  it("現在の拍子を、異なる拍子の区間つきで表す", () => {
    expect(currentMeterLabel(summarizeBeatmap(makeBeatmap()))).toBe("4/4");
    expect(
      currentMeterLabel(
        summarizeBeatmap(
          makeBeatmap({
            time_signatures: [
              { bar: 1, numerator: 4, denominator: 4 },
              { bar: 2, numerator: 2, denominator: 4 },
              { bar: 3, numerator: 4, denominator: 4 },
            ],
            beats: [
              { time_sec: 0.5, beat_in_bar: 1, bar: 1 },
              { time_sec: 1, beat_in_bar: 1, bar: 2 },
              { time_sec: 1.5, beat_in_bar: 1, bar: 3 },
              { time_sec: 2, beat_in_bar: 2, bar: 3 },
            ],
            downbeats_sec: [0.5, 1, 1.5],
          }),
        ),
      ),
    ).toBe("4/4(小節 2 は 2/4)");
  });

  it("拍子が無い場合はダッシュを返す", () => {
    expect(currentMeterLabel(summarizeBeatmap(makeBeatmap({ time_signatures: [] })))).toBe("—");
  });
});
