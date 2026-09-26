import { create } from "zustand";
import {
  DEFAULT_PREVIEW_BARS,
  DEFAULT_ZOOM,
  normalizePreviewBars,
  normalizeZoom,
  readStoredPreviewBars,
  readStoredZoom,
  storePreviewBars,
  storeZoom,
} from "../lib/scoreView";

/**
 * 「先頭N小節プレビュー」の表示設定(#172)。既定は4小節で、変更は保存され
 * 次回起動時も維持される(`themeStore`と同じ作り)。
 */
interface ScoreViewState {
  bars: number;
  setBars: (bars: number) => void;
  /** 楽譜の拡大率(#179)。 */
  zoom: number;
  setZoom: (zoom: number) => void;
}

export const useScoreViewStore = create<ScoreViewState>((set) => ({
  bars: DEFAULT_PREVIEW_BARS,
  setBars: (bars) => {
    const normalized = normalizePreviewBars(bars);
    storePreviewBars(window.localStorage, normalized);
    set({ bars: normalized });
  },
  zoom: DEFAULT_ZOOM,
  setZoom: (zoom) => {
    const normalized = normalizeZoom(zoom);
    storeZoom(window.localStorage, normalized);
    set({ zoom: normalized });
  },
}));

/** 起動時に保存値を読み込む(`main.tsx`が描画前に呼ぶ)。 */
export function initScoreView(): number {
  const bars = readStoredPreviewBars(window.localStorage);
  useScoreViewStore.setState({ bars, zoom: readStoredZoom(window.localStorage) });
  return bars;
}
