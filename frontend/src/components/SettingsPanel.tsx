import { THEME_LABEL, type ThemeMode } from "../lib/theme";
import { useThemeStore } from "../stores/themeStore";

// 枠線は「幅・線種」だけを共通にし、**色は選択状態ごとに排他**で指定する。
// 同じプロパティ(border-*)を両方に置くと、Tailwindは生成CSSの順序で勝敗を決めるため
// 選択状態が意図どおり反映されない(レビュー指摘)。
const OPTION_CLASS = "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm";

/** 選択中のラジオを強調する(テーマ切替の現在値が一目で分かるように)。 */
const SELECTED_CLASS =
  "border-blue-500 dark:border-blue-400 bg-blue-50 dark:bg-blue-950 font-medium";

/** 非選択時の枠線・ホバー(選択時とは排他)。 */
const UNSELECTED_CLASS =
  "border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800";

/**
 * 設定パネル(#152)。現時点ではテーマ(ダーク/ライト)のみ。
 *
 * 既定はダークモード。切替は即座に反映され、localStorageへ保存される。
 */
export function SettingsPanel() {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);

  return (
    <section className="space-y-2 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <h2 className="text-lg font-semibold text-gray-700 dark:text-gray-200">設定</h2>
      <fieldset className="space-y-1">
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-200">画面テーマ</legend>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          既定はダークモードです。設定は保存され、次回起動時も維持されます。
        </p>
        <div className="flex flex-wrap gap-2 pt-1">
          {(["dark", "light"] as const).map((mode: ThemeMode) => (
            <label
              key={mode}
              className={`${OPTION_CLASS} ${theme === mode ? SELECTED_CLASS : UNSELECTED_CLASS}`}
            >
              <input
                type="radio"
                name="theme"
                value={mode}
                checked={theme === mode}
                onChange={() => setTheme(mode)}
              />
              {THEME_LABEL[mode]}
            </label>
          ))}
        </div>
      </fieldset>
    </section>
  );
}
