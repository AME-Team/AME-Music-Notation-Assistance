import type { ReactNode } from "react";
import { GLOSSARY, type GlossaryKey } from "../../lib/glossary";
import { Tooltip } from "./Tooltip";

interface TermProps {
  /** `lib/glossary.ts`のキー。 */
  k: GlossaryKey;
  children: ReactNode;
}

/**
 * UI刷新: 専門用語に点線の下線を引き、ホバー/フォーカスで`GLOSSARY`の説明を出す。
 * 使い方: `<Term k="stem">ステム</Term>`
 */
export function Term({ k, children }: TermProps) {
  return (
    <Tooltip text={GLOSSARY[k]}>
      {/* #7 アクセシビリティ: キーボードでも説明を開けるよう、tabIndex付きのspanでは
          なく実際に操作可能な<button>を使う(biome/lint-a11yの静的要素への
          対話ハンドラ付与の指摘にも合致する)。見た目はテキストと変えず、
          点線の下線だけで「説明がある」ことを示す。 */}
      <button
        type="button"
        className="cursor-help border-b border-dotted border-gray-400 dark:border-gray-500 bg-transparent p-0 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
      >
        {children}
      </button>
    </Tooltip>
  );
}
