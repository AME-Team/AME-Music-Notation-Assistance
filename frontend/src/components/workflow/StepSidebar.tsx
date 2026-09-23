import { STEPS, type StepId, type StepState } from "../../lib/workflow";

const STATUS_ICON: Record<StepState["status"], string> = {
  done: "✓",
  current: "●",
  upcoming: "○",
  locked: "○",
  stale: "⚠",
  skipped: "—",
};

const STATUS_TEXT_CLASS: Record<StepState["status"], string> = {
  done: "text-emerald-600 dark:text-emerald-400",
  current: "text-blue-600 dark:text-blue-400",
  upcoming: "text-gray-400 dark:text-gray-500",
  locked: "text-gray-300 dark:text-gray-600",
  stale: "text-amber-600 dark:text-amber-400",
  skipped: "text-gray-400 dark:text-gray-500",
};

interface StepSidebarProps {
  states: StepState[];
  activeStep: StepId;
  onSelect: (id: StepId) => void;
  onRunRemaining: () => void;
  canRunRemaining: boolean;
  isRunningRemaining: boolean;
}

/**
 * UI刷新: 画面左のステップ一覧。作業の順序を「①〜⑦」として常に見える形にする
 * (以前は全機能を縦一列に並べているだけで、次に何をすべきか画面から読み取れ
 * なかった)。クリックで完了済み/現在のステップへ移動できる。ロック中の
 * ステップは選択できない(前のステップが終わっていないため)。
 */
export function StepSidebar({
  states,
  activeStep,
  onSelect,
  onRunRemaining,
  canRunRemaining,
  isRunningRemaining,
}: StepSidebarProps) {
  return (
    <nav
      aria-label="作業ステップ"
      className="w-56 shrink-0 space-y-4 rounded-lg border border-gray-200 dark:border-gray-700 p-3"
    >
      <ol className="space-y-1">
        {STEPS.map((step, index) => {
          const state = states.find((s) => s.id === step.id);
          const status = state?.status ?? "locked";
          const isSelectable = status !== "locked";
          const isActive = step.id === activeStep;
          return (
            <li key={step.id}>
              <button
                type="button"
                onClick={() => isSelectable && onSelect(step.id)}
                disabled={!isSelectable}
                aria-current={isActive ? "step" : undefined}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors duration-150 ease-out disabled:cursor-not-allowed ${
                  isActive
                    ? "bg-blue-50 dark:bg-blue-950 font-medium text-blue-800 dark:text-blue-200"
                    : isSelectable
                      ? "text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
                      : "text-gray-400 dark:text-gray-600"
                }`}
              >
                <span className={`w-4 text-center ${STATUS_TEXT_CLASS[status]}`} aria-hidden="true">
                  {STATUS_ICON[status]}
                </span>
                <span>
                  {index + 1}. {step.title}
                  {step.optional && (
                    <span className="ml-1 text-xs text-gray-400 dark:text-gray-500">(任意)</span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <button
        type="button"
        onClick={onRunRemaining}
        disabled={!canRunRemaining || isRunningRemaining}
        className="w-full rounded-md bg-gray-100 dark:bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400 disabled:opacity-50"
      >
        {isRunningRemaining ? "自動実行中..." : "①〜④を残りまとめて実行"}
      </button>
    </nav>
  );
}
