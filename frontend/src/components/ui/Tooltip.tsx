import { type ReactNode, useEffect, useId, useRef, useState } from "react";

const SHOW_DELAY_MS = 300;

interface TooltipProps {
  /** 吹き出しに表示する説明文。 */
  text: string;
  children: ReactNode;
  /** トリガー要素に付与する追加クラス(下線など見た目の調整用)。 */
  triggerClassName?: string;
}

/**
 * UI刷新: 専門用語やボタンにホバー/キーボードフォーカスで説明を出す共通コンポーネント。
 *
 * ネイティブの`title`属性は表示までの遅延やスタイリングを制御できないため使わず、
 * 自前のポップオーバーにする。表示は300ms遅延、`role="tooltip"`+`aria-describedby`で
 * スクリーンリーダーにも伝え、画面右端では吹き出しの向きを反転させる。
 */
export function Tooltip({ text, children, triggerClassName }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const [align, setAlign] = useState<"left" | "right">("left");
  const showTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const triggerRef = useRef<HTMLSpanElement>(null);
  const tooltipId = useId();

  function scheduleShow() {
    if (showTimer.current) clearTimeout(showTimer.current);
    showTimer.current = setTimeout(() => setVisible(true), SHOW_DELAY_MS);
  }

  function hide() {
    if (showTimer.current) clearTimeout(showTimer.current);
    setVisible(false);
  }

  useEffect(() => {
    if (!visible || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const overflowsRight = rect.left + 160 > window.innerWidth;
    setAlign(overflowsRight ? "right" : "left");
  }, [visible]);

  useEffect(
    () => () => {
      if (showTimer.current) clearTimeout(showTimer.current);
    },
    [],
  );

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: 対話的な意味(role/tabIndex)はchildren側(トリガー要素自身)が持つ。ここはホバー/フォーカスの検出領域を広げるだけの純粋なラッパー。
    <span
      ref={triggerRef}
      className={`relative inline-block ${triggerClassName ?? ""}`}
      onMouseEnter={scheduleShow}
      onMouseLeave={hide}
      onFocus={scheduleShow}
      onBlur={hide}
      aria-describedby={visible ? tooltipId : undefined}
    >
      {children}
      {visible && (
        <span
          id={tooltipId}
          role="tooltip"
          className={`pointer-events-none absolute top-full z-50 mt-1.5 w-max max-w-56 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-2.5 py-1.5 text-xs font-normal text-gray-700 dark:text-gray-200 shadow-sm transition-opacity duration-150 ease-out ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          {text}
        </span>
      )}
    </span>
  );
}
