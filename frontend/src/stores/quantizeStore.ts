import { create } from "zustand";
import {
  DEFAULT_QUANTIZE_SETTINGS,
  normalizeQuantizeSettings,
  type QuantizeSettings,
  readStoredQuantizeSettings,
  storeQuantizeSettings,
} from "../lib/quantizeSettings";

/**
 * 量子化の適用設定(#174)。既定は16分音符・強さ100%・有効で、変更は保存され
 * 次回起動時も維持される(`scoreViewStore`と同じ作り)。
 */
interface QuantizeState {
  settings: QuantizeSettings;
  update: (patch: Partial<QuantizeSettings>) => void;
  reset: () => void;
}

export const useQuantizeStore = create<QuantizeState>((set, get) => ({
  settings: { ...DEFAULT_QUANTIZE_SETTINGS },
  update: (patch) => {
    const next = normalizeQuantizeSettings({ ...get().settings, ...patch });
    storeQuantizeSettings(window.localStorage, next);
    set({ settings: next });
  },
  reset: () => {
    const next = { ...DEFAULT_QUANTIZE_SETTINGS };
    storeQuantizeSettings(window.localStorage, next);
    set({ settings: next });
  },
}));

/**
 * 起動時に保存値を読み込む(`main.tsx`が描画前に呼ぶ)。
 *
 * ステージ実行のパラメータは、実行の直前にもこれで最新値を取り出す
 * (古いクロージャの値を送らないため)。
 */
export function initQuantizeSettings(): QuantizeSettings {
  const settings = readStoredQuantizeSettings(window.localStorage);
  useQuantizeStore.setState({ settings });
  return settings;
}
