import { describe, expect, it } from "vitest";
import { peaksToBars } from "./waveform";

describe("peaksToBars", () => {
  it("maps [min, max] pairs to rects in a 0..2 coordinate space", () => {
    const bars = peaksToBars([
      [-0.5, 0.5],
      [-1, 1],
    ]);
    expect(bars).toEqual([
      { x: 0, y: 0.5, width: 1, height: 1 },
      { x: 1, y: 0, width: 1, height: 2 },
    ]);
  });

  it("clamps near-silent buckets to a minimum visible height", () => {
    const [bar] = peaksToBars([[0, 0]]);
    expect(bar.height).toBeGreaterThan(0);
  });

  it("assigns sequential x positions matching bucket index", () => {
    const bars = peaksToBars([
      [0, 0.1],
      [0, 0.2],
      [0, 0.3],
    ]);
    expect(bars.map((b) => b.x)).toEqual([0, 1, 2]);
  });
});
