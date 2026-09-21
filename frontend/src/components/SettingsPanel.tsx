import { THEME_LABEL, type ThemeMode } from "../lib/theme";
import { useThemeStore } from "../stores/themeStore";

const OPTION_CLASS =
  "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm " +
  "border-gray-300 dark:border-gray-600 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-900 dark:hover:bg-gray-800";

/** 選択中のラジオを強調する(テーマ切替の現在値が一目で分かるように)。 */
const SELECTED_CLASS = "border-blue-500 bg-blue-50 dark:bg-blue-950 dark:bg-blue-950 font-medium";

/**
 * 設定パネル(#152)。現時点ではテーマ(ダーク/ライト)のみ。
 *
 * 既定はダークモード。切替は即座に反映され、localStorageへ保存される。
 */
export function SettingsPanel() {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);

  return (
    <section className="space-y-2 rounded-lg border border-gray-200 dark:border-gray-700 p-4 dark:border-gray-700">
      <h2 className="text-lg font-semibold text-gray-700 dark:text-gray-200 dark:text-gray-200">
        設定
      </h2>
      <fieldset className="space-y-1">
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-200 dark:text-gray-200">
          画面テーマ
        </legend>
        <p className="text-xs text-gray-500 dark:text-gray-400 dark:text-gray-400">
          既定はダークモードです。設定は保存され、次回起動時も維持されます。
        </p>
        <div className="flex flex-wrap gap-2 pt-1">
          {(["dark", "light"] as const).map((mode: ThemeMode) => (
            <label key={mode} className={`${OPTION_CLASS} ${theme === mode ? SELECTED_CLASS : ""}`}>
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
