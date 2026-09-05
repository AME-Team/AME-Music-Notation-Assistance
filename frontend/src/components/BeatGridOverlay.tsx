import type { Beatmap } from "../api/client";
import { computeBeatGridLines } from "../lib/beatGrid";

interface BeatGridOverlayProps {
  beatmap: Beatmap;
  durationSec: number;
}

/**
 * #18/#20: ビート・ダウンビート・小節線を波形上に重畳表示する。
 *
 * 親要素(`Waveform` を含むコンテナ)が `relative` であることを前提に、
 * `absolute inset-0` で重ねる。`pointer-events-none` によりクリック/ドラッグ操作は
 * 下の波形(シーク等)へ素通しする。
 */
export function BeatGridOverlay({ beatmap, durationSec }: BeatGridOverlayProps) {
  if (durationSec <= 0) return null;
  const lines = computeBeatGridLines(beatmap);

  return (
    <svg
      viewBox={`0 0 ${durationSec} 1`}
      preserveAspectRatio="none"
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden="true"
    >
      {lines.map((line) => (
        <line
          key={line.timeSec}
          x1={line.timeSec}
          x2={line.timeSec}
          y1={0}
          y2={1}
          vectorEffect="non-scaling-stroke"
          className={line.isDownbeat ? "stroke-gray-700" : "stroke-gray-400"}
          strokeWidth={line.isDownbeat ? 2 : 1}
          opacity={line.isDownbeat ? 0.8 : 0.4}
        />
      ))}
    </svg>
  );
}
