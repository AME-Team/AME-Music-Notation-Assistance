import { useEffect, useRef } from "react";
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
 * フォーカストラップの対象(ダイアログ内で Tab 移動できる要素)。
 *
 * - `tabindex="-1"` は除外する(ダイアログ自身が該当するため)
 * - **ラジオはタブ順に入るのはチェック済みの1つだけ**なので、未チェックのラジオを
 *   対象に含めると「末尾」の判定が実際のタブ順とずれ、Tabで背景へ抜ける
 *   (レビュー指摘: 既定ダーク=先頭がチェック済みのとき、末尾と比較しても一致せず
 *   トラップが効かなかった)
 */
const FOCUSABLE_SELECTOR = [
  "button",
  "[href]",
  'input:not([type="radio"])',
  'input[type="radio"]:checked',
  "select",
  "textarea",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

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
 */
export function SettingsModal({ open, onClose }: SettingsModalProps) {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const dialogRef = useRef<HTMLDivElement>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  // 最新の`onClose`をrefで参照する。依存配列に入れると、呼び出し側がインライン関数
  // (`onClose={() => setSettingsOpen(false)}`)を渡している場合に**再レンダリングの
  // たびeffectが再実行される**。再実行時のcleanupは「元の要素へフォーカスを戻す」
  // 処理なので、モーダル表示中にフォーカスがダイアログの外へ出てしまい、以降の
  // 復元先もダイアログ自身に化ける(レビュー指摘)。
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!open) return;
    lastFocusedRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      // フォーカストラップ: `aria-modal="true"`は「背景は操作できない」ことを示すが、
      // Tabキーは背景の要素へ抜けてしまうため、ダイアログ内で循環させる
      // (レビュー指摘。背景を`inert`にする方法もあるが、モーダルはAppのDOM内に
      // 描画しているため、その場合はポータル化とセットになる)。
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (element) => !element.hasAttribute("disabled"),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      // フォーカスが既にダイアログの外にある場合(背景クリック等)は、まず内側へ戻す。
      if (!(active instanceof HTMLElement) || !dialog.contains(active)) {
        event.preventDefault();
        first.focus();
        return;
      }
      if (event.shiftKey) {
        if (active === first || active === dialog) {
          event.preventDefault();
          last.focus();
        }
        return;
      }
      if (active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    // 背景クリックで閉じる。オーバーレイ自体にハンドラを付けると
    // 静的な要素がインタラクティブ扱いになる(a11y lint)ため、
    // document側で「ダイアログの外側かどうか」を見て判定する。
    const onMouseDown = (event: MouseEvent) => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      if (event.target instanceof Node && !dialog.contains(event.target)) onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
      lastFocusedRef.current?.focus();
    };
  }, [open]);

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
      </div>
    </div>
  );
}
