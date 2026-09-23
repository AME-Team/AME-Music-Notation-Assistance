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
/**
 * Gate2レビュー指摘(MIDDLE): 以前は`<button>`をトリガー要素にしていたが、
 * `<button>`は`<label>`の"labelable"な要素であるため、
 * `<label><Term>ラベル文</Term><select/></label>`のように`<select>`と
 * 同じ`<label>`内に置くと、ブラウザの暗黙のlabel関連付けは最初に見つかった
 * labelable要素(=このbutton)に奪われ、本来関連付けるべき`<select>`/
 * `<input>`との紐付けが失われる(SeparateStep/AgentTaskLauncherの各ラベルで
 * 実際に発生していた)。labelableではない`<span>`へ戻し、キーボード到達性は
 * `tabIndex`で確保する(#7)。ここは対話操作(クリックで何かを実行する)では
 * なく補足情報の開示のみのため、button要素が持つ「アクションを実行する」
 * という暗黙の意味論はそもそも不適切だった。
 */
export function Term({ k, children }: TermProps) {
  return (
    <Tooltip text={GLOSSARY[k]}>
      <span
        // biome-ignore lint/a11y/noNoninteractiveTabindex: 補足情報を開示するだけのspanで、labelableなbutton/aは`<label>`内で誤って関連付けを奪うため使えない(詳細は関数コメント参照)。ホバー/フォーカスで開くWAI-ARIA tooltipパターンとして許容される。
        tabIndex={0}
        className="cursor-help border-b border-dotted border-gray-400 dark:border-gray-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
      >
        {children}
      </span>
    </Tooltip>
  );
}
