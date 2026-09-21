import { create } from "zustand";
import {
  DEFAULT_THEME,
  otherTheme,
  readStoredTheme,
  storeTheme,
  type ThemeMode,
} from "../lib/theme";

/**
 * テーマ状態(#152)。既定はダーク。設定変更は即座に`<html>`へ反映し、
 * localStorageへ保存する(次回起動時も維持)。
 */
interface ThemeState {
  theme: ThemeMode;
  setTheme: (theme: ThemeMode) => void;
  toggleTheme: () => void;
}

/** `<html>`へテーマを適用する(DOM操作はここに集約)。 */
export function applyThemeToDocument(theme: ThemeMode): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.dataset.theme = theme;
  // フォーム部品(スクロールバー等)のブラウザ既定配色も合わせる。
  root.style.colorScheme = theme;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: DEFAULT_THEME,
  setTheme: (theme) => {
    applyThemeToDocument(theme);
    storeTheme(window.localStorage, theme);
    set({ theme });
  },
  toggleTheme: () => get().setTheme(otherTheme(get().theme)),
}));

/**
 * 起動時に保存値を読み込んで適用する(`main.tsx`が描画前に呼ぶ)。
 *
 * 描画前に適用することで、起動直後に一瞬ライトで表示されるのを避ける。
 */
export function initTheme(): ThemeMode {
  const theme = readStoredTheme(window.localStorage);
  applyThemeToDocument(theme);
  useThemeStore.setState({ theme });
  return theme;
}
