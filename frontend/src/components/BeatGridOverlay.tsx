import type { Beatmap } from "../api/client";
import { computeBeatGridLines } from "../lib/beatGrid";
import { type TimeView, viewSpanSec } from "../lib/waveformView";

interface BeatGridOverlayProps {
  beatmap: Beatmap;
  durationSec: number;
  /** 波形と同じ表示区間(秒)。片方だけ更新すると重なりがズレるため必須にしている。 */
  view: TimeView;
}

/**
 * #18/#20: ビート・ダウンビート・小節線を波形上に重畳表示する。
 *
 * `viewBox`の横方向を**表示区間(秒)**のまま使うため(#169)、波形側とまったく同じ
 * 座標変換になる。拡大しても線の太さは`vectorEffect`で一定に保つ。
 *
 * 親要素(`Waveform` を含むコンテナ)が `relative` であることを前提に、
 * `absolute inset-0` で重ねる。`pointer-events-none` によりクリック/ドラッグ操作は
 * 下の波形(シーク等)へ素通しする。
 */
export function BeatGridOverlay({ beatmap, durationSec, view }: BeatGridOverlayProps) {
  const span = viewSpanSec(view);
  if (durationSec <= 0 || span <= 0) return null;
  const lines = computeBeatGridLines(beatmap);

  return (
    <svg
      viewBox={`${view.startSec} 0 ${span} 1`}
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
