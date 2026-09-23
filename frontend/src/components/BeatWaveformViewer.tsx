import { useEffect, useRef, useState } from "react";
import type { Beatmap } from "../api/client";
import { formatSeconds } from "../lib/beatSummary";
import {
  fullView,
  panFraction,
  panView,
  pointDurationSec,
  rulerTicks,
  type TimeView,
  viewFromFraction,
  viewSpanSec,
  ZOOM_STEP,
  zoomFactor,
  zoomView,
} from "../lib/waveformView";
import { BeatGridOverlay } from "./BeatGridOverlay";
import { Waveform } from "./Waveform";

interface BeatWaveformViewerProps {
  peaks: number[][];
  durationSec: number;
  beatmap: Beatmap | null;
  view: TimeView;
  onViewChange: (view: TimeView) => void;
  height?: number;
}

/** 幅を測る前(初回レンダー)に目盛りを間引くための想定幅。 */
const FALLBACK_WIDTH_PX = 900;

/**
 * #169: ビートグリッド補正用の波形ビュー(ズーム・横スクロール付き)。
 *
 * 以前は全曲をそのまま横幅へ圧縮して描いていたため、3分程度の曲では1拍が
 * 数ピクセルになり、「拍の頭が波形のどこに乗っているか」を目で確認できず、
 * 全体オフセットの補正が事実上できなかった。表示区間(`view`)を明示的に持ち、
 * 拡大(ズーム)と横スクロールで任意の区間を等倍以上で見られるようにする。
 *
 * 操作:
 * - 「＋」「−」ボタン: 表示中央を固定して拡大/縮小
 * - 「全体表示」: 全曲表示へ戻す
 * - ホイール: 横スクロール / Ctrl(⌘)+ホイール: ポインタ位置を固定して拡大
 * - 下部のスライダ: 横スクロール(キーボードの←→でも動く)
 */
export function BeatWaveformViewer({
  peaks,
  durationSec,
  beatmap,
  view,
  onViewChange,
  height = 128,
}: BeatWaveformViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [widthPx, setWidthPx] = useState(FALLBACK_WIDTH_PX);

  const span = viewSpanSec(view);
  const factor = zoomFactor(view, durationSec);
  const granularityMs = Math.round(pointDurationSec(peaks.length, durationSec) * 1000);
  const scrollable = span < durationSec - 1e-6;
  const ticks = rulerTicks(view, widthPx);

  // 目盛りの間引きは実際の表示幅で決めるため、幅を測って追従させる。
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    const update = () => setWidthPx(element.clientWidth || FALLBACK_WIDTH_PX);
    update();
    if (typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Reactの`onWheel`はパッシブ登録になり`preventDefault`できないため、DOMへ直接登録する
  // (`preventDefault`しないと、ホイールで拡大したついでに画面全体がスクロールしてしまう)。
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;

    function handleWheel(event: WheelEvent) {
      const rect = element?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      event.preventDefault();

      const timeAtCursor = view.startSec + ((event.clientX - rect.left) / rect.width) * span;
      if (event.ctrlKey || event.metaKey) {
        // ポインタ位置を固定して拡大/縮小する。
        const zoomIn = event.deltaY < 0;
        onViewChange(zoomView(view, zoomIn ? ZOOM_STEP : 1 / ZOOM_STEP, timeAtCursor, durationSec));
        return;
      }

      // 横スクロール(トラックパッドの横成分も拾う)。
      const deltaPx = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      onViewChange(panView(view, (deltaPx / rect.width) * span, durationSec));
    }

    element.addEventListener("wheel", handleWheel, { passive: false });
    return () => element.removeEventListener("wheel", handleWheel);
  }, [view, span, durationSec, onViewChange]);

  function zoomAtCenter(nextFactor: number) {
    onViewChange(zoomView(view, nextFactor, view.startSec + span / 2, durationSec));
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-gray-600 text-sm dark:text-gray-300">
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="ズームアウト"
            onClick={() => zoomAtCenter(1 / ZOOM_STEP)}
            className="h-7 w-7 rounded-md border border-gray-300 text-base leading-none hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            −
          </button>
          <button
            type="button"
            aria-label="ズームイン"
            onClick={() => zoomAtCenter(ZOOM_STEP)}
            className="h-7 w-7 rounded-md border border-gray-300 text-base leading-none hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            ＋
          </button>
          <button
            type="button"
            onClick={() => onViewChange(fullView(durationSec))}
            disabled={!scrollable}
            className="rounded-md border border-gray-300 px-2 py-1 text-xs hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            全体表示
          </button>
        </div>
        <span className="text-xs">
          表示幅 {formatSeconds(span)}(全体 {formatSeconds(durationSec)}・倍率 {factor.toFixed(1)}×)
        </span>
        <span className="text-gray-500 text-xs dark:text-gray-400">
          波形の粒度 {granularityMs} ms/点
        </span>
      </div>

      <div className="overflow-hidden rounded-md border border-gray-200 dark:border-gray-700">
        <div ref={containerRef} className="relative">
          <div className="relative h-4 border-gray-200 border-b dark:border-gray-700">
            {ticks.map((tick) => (
              <span
                key={tick.timeSec}
                className="pointer-events-none absolute top-0.5 -translate-x-1/2 text-[10px] text-gray-400 dark:text-gray-500"
                style={{ left: `${tick.ratio * 100}%` }}
              >
                {tick.label}
              </span>
            ))}
          </div>
          <div className="relative">
            <Waveform peaks={peaks} durationSec={durationSec} view={view} height={height} />
            {beatmap && <BeatGridOverlay beatmap={beatmap} durationSec={durationSec} view={view} />}
          </div>
        </div>
        <div className="flex items-center gap-2 border-gray-200 border-t px-2 py-1 dark:border-gray-700">
          <span className="text-[10px] text-gray-400 dark:text-gray-500">横スクロール</span>
          <input
            type="range"
            aria-label="横スクロール"
            min={0}
            max={1000}
            step={1}
            value={Math.round(panFraction(view, durationSec) * 1000)}
            disabled={!scrollable}
            onChange={(event) =>
              onViewChange(viewFromFraction(Number(event.target.value) / 1000, span, durationSec))
            }
            className="h-1 flex-1 cursor-pointer accent-blue-600 disabled:opacity-50"
          />
        </div>
      </div>
    </div>
  );
}
