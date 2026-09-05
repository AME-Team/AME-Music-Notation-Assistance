import { peaksToBars } from "../lib/waveform";

interface WaveformProps {
  peaks: number[][];
  height?: number;
}

/** #21: ピークデータ(min/maxをブロック単位で事前計算済み)を軽量なSVG棒グラフとして描画する。 */
export function Waveform({ peaks, height = 96 }: WaveformProps) {
  const bars = peaksToBars(peaks);

  return (
    <svg
      viewBox={`0 0 ${Math.max(bars.length, 1)} 2`}
      preserveAspectRatio="none"
      className="w-full"
      style={{ height }}
      role="img"
      aria-label="波形"
    >
      {bars.map((bar) => (
        <rect
          key={bar.x}
          x={bar.x}
          y={bar.y}
          width={bar.width}
          height={bar.height}
          className="fill-blue-300"
        />
      ))}
    </svg>
  );
}
