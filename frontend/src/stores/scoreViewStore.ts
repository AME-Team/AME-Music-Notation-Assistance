import { create } from "zustand";
import type { ScoreLayout } from "../lib/scoreView";
import {
  DEFAULT_PREVIEW_BARS,
  DEFAULT_SCORE_LAYOUT,
  DEFAULT_ZOOM,
  normalizePreviewBars,
  normalizeScoreLayout,
  normalizeZoom,
  readStoredPreviewBars,
  readStoredScoreLayout,
  readStoredZoom,
  storePreviewBars,
  storeScoreLayout,
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
  /** 5線譜の並べ方(#181)。既定は1本の横方向の段。 */
  layout: ScoreLayout;
  setLayout: (layout: ScoreLayout) => void;
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
  layout: DEFAULT_SCORE_LAYOUT,
  setLayout: (layout) => {
    const normalized = normalizeScoreLayout(layout);
    storeScoreLayout(window.localStorage, normalized);
    set({ layout: normalized });
  },
}));

/** 起動時に保存値を読み込む(`main.tsx`が描画前に呼ぶ)。 */
export function initScoreView(): number {
  const bars = readStoredPreviewBars(window.localStorage);
  useScoreViewStore.setState({
    bars,
    zoom: readStoredZoom(window.localStorage),
    layout: readStoredScoreLayout(window.localStorage),
  });
  return bars;
}
