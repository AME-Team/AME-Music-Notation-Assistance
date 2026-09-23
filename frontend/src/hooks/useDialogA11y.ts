import { type RefObject, useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "button",
  "[href]",
  'input:not([type="radio"])',
  'input[type="radio"]:checked',
  "select",
  "textarea",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

/**
 * `SettingsModal`(#158)で確立したダイアログのアクセシビリティ配線
 * (Escapeで閉じる/背景クリックで閉じる/Tabのフォーカストラップ/開閉時の
 * フォーカス移動と復元)を、`JobDetailModal`など他のモーダルでも再利用する
 * ための共通フック。ロジック自体はSettingsModalから抽出したもので変更しない。
 */
export function useDialogA11y(
  dialogRef: RefObject<HTMLDivElement | null>,
  open: boolean,
  onClose: () => void,
): void {
  const lastFocusedRef = useRef<HTMLElement | null>(null);
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
  }, [open, dialogRef]);
}
