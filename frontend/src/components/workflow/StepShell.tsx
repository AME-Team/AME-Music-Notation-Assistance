import type { ReactNode } from "react";
import type { StepId } from "../../lib/workflow";
import { stepDef } from "../../lib/workflow";

interface StepShellProps {
  stepId: StepId;
  stepIndex: number;
  children: ReactNode;
  onBack?: () => void;
  onNext?: () => void;
  nextDisabledReason?: string;
  /** 「次へ」の代わりに表示するボタン文言(既定:「次へ」)。 */
  nextLabel?: string;
}

/**
 * UI刷新: 各ステップ画面に共通の見出し・説明文・戻る/次へナビゲーションの
 * 外枠。中身(実行ボタンや結果表示)は`components/workflow/steps/*.tsx`が
 * 個別に用意し、`children`として渡す。
 */
export function StepShell({
  stepId,
  stepIndex,
  children,
  onBack,
  onNext,
  nextDisabledReason,
  nextLabel = "次へ",
}: StepShellProps) {
  const def = stepDef(stepId);
  return (
    <div className="flex-1 space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          {stepIndex}. {def.title}
        </h2>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{def.description}</p>
      </div>

      <div className="space-y-4">{children}</div>

      {(onBack || onNext) && (
        <div className="flex items-center justify-between border-t border-gray-100 dark:border-gray-800 pt-4">
          <div>
            {onBack && (
              <button
                type="button"
                onClick={onBack}
                className="rounded-md px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400"
              >
                ← 戻る
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            {nextDisabledReason && (
              <span className="text-xs text-gray-400 dark:text-gray-500">{nextDisabledReason}</span>
            )}
            {onNext && (
              <button
                type="button"
                onClick={onNext}
                disabled={Boolean(nextDisabledReason)}
                className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
              >
                {nextLabel} →
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
