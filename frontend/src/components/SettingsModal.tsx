import { useRef } from "react";
import { useDialogA11y } from "../hooks/useDialogA11y";
import { PREVIEW_BARS_CHOICES } from "../lib/scoreView";
import { THEME_LABEL, type ThemeMode } from "../lib/theme";
import { useScoreViewStore } from "../stores/scoreViewStore";
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

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
}

/**
 * 設定モーダル(#158)。現時点ではテーマ(ダーク/ライト)のみ。
 *
 * メインウィンドウに常設せず、ネイティブメニュー「編集 > 設定」から開く。
 * 閉じる経路は3つ: 「閉じる」ボタン / Escapeキー / 背景クリック。
 *
 * 開いたときにダイアログへフォーカスを移し、閉じたら元の要素へ戻す
 * (キーボード操作で開いたときにフォーカスが背景へ残らないようにする)。
 * このフォーカス管理・トラップ・Escape/背景クリックの配線は
 * `hooks/useDialogA11y.ts`に抽出し、`JobDetailModal`と共有している。
 */
export function SettingsModal({ open, onClose }: SettingsModalProps) {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const previewBars = useScoreViewStore((s) => s.bars);
  const setPreviewBars = useScoreViewStore((s) => s.setBars);
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogA11y(dialogRef, open, onClose);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-16">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-modal-title"
        tabIndex={-1}
        className="w-full max-w-md space-y-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-4 shadow-xl"
      >
        <div className="flex items-center justify-between">
          <h2
            id="settings-modal-title"
            className="text-lg font-semibold text-gray-900 dark:text-gray-100"
          >
            設定
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            閉じる
          </button>
        </div>
        <fieldset className="space-y-1">
          <legend className="text-sm font-medium text-gray-700 dark:text-gray-200">
            画面テーマ
          </legend>
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

        <fieldset className="space-y-1">
          <legend className="text-sm font-medium text-gray-700 dark:text-gray-200">
            先頭N小節プレビュー
          </legend>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            すべての作業ページに出す「先頭N小節のMIDIと楽譜」の小節数です(既定は4小節)。
          </p>
          <div className="flex flex-wrap gap-2 pt-1">
            {PREVIEW_BARS_CHOICES.map((value) => (
              <label
                key={value}
                className={`${OPTION_CLASS} ${previewBars === value ? SELECTED_CLASS : UNSELECTED_CLASS}`}
              >
                <input
                  type="radio"
                  name="preview-bars"
                  value={value}
                  checked={previewBars === value}
                  onChange={() => setPreviewBars(value)}
                />
                {value} 小節
              </label>
            ))}
          </div>
        </fieldset>
      </div>
    </div>
  );
}
