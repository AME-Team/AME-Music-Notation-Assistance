import { peaksToBars } from "../lib/waveform";
import { type TimeView, viewSpanSec, visiblePointRange } from "../lib/waveformView";

interface WaveformProps {
  peaks: number[][];
  /** 音源の長さ(秒)。時間軸(秒)で描画するために必要。 */
  durationSec: number;
  /** 表示区間(秒)。ズームと横スクロールはこの区間で表す。 */
  view: TimeView;
  height?: number;
}

/**
 * #21/#169: ピークデータ(min/maxをブロック単位で事前計算済み)を軽量なSVG棒グラフとして描画する。
 *
 * `viewBox`を**表示区間**にするため、拡大しても波形のスケールはビート線
 * (`BeatGridOverlay`)と目盛りと一致する。以前は全曲を横幅へそのまま圧縮していたため、
 * 3分程度の曲では1拍が数ピクセルになり、波形を見ながら全体オフセットを決められなかった。
 */
export function Waveform({ peaks, durationSec, view, height = 96 }: WaveformProps) {
  const pointSec = durationSec / Math.max(peaks.length, 1);
  // 表示区間に入る点だけを描く(全点を描くと拡大時に無駄なDOMが増える)。
  const { start, end } = visiblePointRange(peaks.length, view, durationSec);
  const bars = peaksToBars(peaks.slice(start, end)).map((bar) => ({
    ...bar,
    // 点の添字を秒へ写像してから、切り出したぶんの開始位置を足す。
    x: (start + bar.x) * pointSec,
    width: pointSec,
  }));

  return (
    <svg
      viewBox={`${view.startSec} 0 ${Math.max(viewSpanSec(view), Number.EPSILON)} 2`}
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
