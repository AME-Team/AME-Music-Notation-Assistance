import { useRef, useState } from "react";
import { useDialogA11y } from "../hooks/useDialogA11y";
import { stageLabel, stepLabel } from "../lib/jobSteps";
import type { TrackedJob } from "../stores/jobStore";

const STATUS_LABEL: Record<string, string> = {
  queued: "待機中",
  running: "実行中",
  succeeded: "完了",
  failed: "失敗",
  cancelled: "キャンセル済み",
};

function formatElapsed(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000));
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return min > 0 ? `${min}分${sec}秒` : `${sec}秒`;
}

function formatClock(at: number): string {
  return new Date(at).toLocaleTimeString("ja-JP", { hour12: false });
}

interface JobDetailModalProps {
  job: TrackedJob | null;
  onClose: () => void;
  onCancel: (jobId: string) => void;
}

/**
 * UI刷新: 「採譜を実行」「量子化を実行」等、時間がかかる処理の詳細を見るための
 * モーダル。`JobMonitor`のトーストと、ワークフロー各ステップの実行中表示の
 * 両方から開けるようにする(`job`を渡すだけで表示するprops駆動)。
 *
 * - ステップのチェックリスト(`step_total`が届いていれば表示)
 * - 全体進捗バー・経過時間
 * - 時刻付きログ(折りたたみ)
 * - 失敗時のエラー詳細とコピー
 */
export function JobDetailModal({ job, onClose, onCancel }: JobDetailModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const open = job !== null;
  useDialogA11y(dialogRef, open, onClose);
  const [copied, setCopied] = useState(false);

  if (!job) return null;

  const isTerminal =
    job.status === "succeeded" || job.status === "failed" || job.status === "cancelled";
  const elapsedMs = (job.finishedAt ?? Date.now()) - job.startedAt;
  const currentStepLabel = stepLabel(job.stage, job.step);

  async function handleCopyError() {
    if (!job?.message) return;
    try {
      await navigator.clipboard.writeText(job.message);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // クリップボードAPIが使えない環境(権限拒否等)では静かに諦める。
      // エラー詳細はモーダル内に既に表示されているため、コピー不可でも
      // 手動選択でコピーできる。
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-16">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="job-detail-modal-title"
        tabIndex={-1}
        className="w-full max-w-lg space-y-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-4 shadow-xl"
      >
        <div className="flex items-center justify-between">
          <h2
            id="job-detail-modal-title"
            className="text-lg font-semibold text-gray-900 dark:text-gray-100"
          >
            {stageLabel(job.stage)}の処理状況
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            閉じる
          </button>
        </div>

        <div className="flex items-center justify-between text-sm">
          <span className="font-medium text-gray-700 dark:text-gray-200">
            {STATUS_LABEL[job.status] ?? job.status}
            {currentStepLabel && job.status === "running" && `: ${currentStepLabel}`}
          </span>
          <span className="text-gray-500 dark:text-gray-400">経過 {formatElapsed(elapsedMs)}</span>
        </div>

        <div className="h-2 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
          <div
            className={`h-full rounded-full transition-all duration-150 ease-out ${
              job.status === "failed" ? "bg-red-500" : "bg-blue-500"
            }`}
            style={{ width: `${Math.round(job.progress * 100)}%` }}
          />
        </div>

        {job.stepTotal && job.stepTotal > 1 && (
          <ol className="space-y-1 text-sm">
            {Array.from({ length: job.stepTotal }, (_, i) => i + 1).map((index) => {
              const isDone = (job.stepIndex ?? 0) > index || job.status === "succeeded";
              const isCurrent = job.stepIndex === index && job.status === "running";
              return (
                <li key={index} className="flex items-center gap-2">
                  <span
                    className={
                      isDone
                        ? "text-emerald-600 dark:text-emerald-400"
                        : isCurrent
                          ? "text-blue-600 dark:text-blue-400"
                          : "text-gray-300 dark:text-gray-600"
                    }
                    aria-hidden="true"
                  >
                    {isDone ? "✓" : isCurrent ? "●" : "○"}
                  </span>
                  <span
                    className={
                      isCurrent
                        ? "font-medium text-gray-900 dark:text-gray-100"
                        : "text-gray-500 dark:text-gray-400"
                    }
                  >
                    工程 {index}/{job.stepTotal}
                    {isCurrent && currentStepLabel ? `: ${currentStepLabel}` : ""}
                  </span>
                </li>
              );
            })}
          </ol>
        )}

        {job.status === "failed" && job.message && (
          <div className="space-y-2 rounded-md border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 p-3 text-sm text-red-700 dark:text-red-300">
            <div className="flex items-center justify-between">
              <p className="font-semibold">エラーの詳細</p>
              <button
                type="button"
                onClick={() => void handleCopyError()}
                className="rounded-md px-2 py-0.5 text-xs text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-red-500"
              >
                {copied ? "コピーしました" : "コピー"}
              </button>
            </div>
            <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap font-mono text-xs">
              {job.message}
            </pre>
          </div>
        )}

        {job.events.length > 0 && (
          <details className="rounded-md border border-gray-200 dark:border-gray-700 p-2 text-sm">
            <summary className="cursor-pointer select-none text-gray-600 dark:text-gray-300">
              ログを見る({job.events.length}件)
            </summary>
            <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto font-mono text-xs text-gray-500 dark:text-gray-400">
              {job.events.map((entry, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: 同一ジョブ内でも同時刻・同stepのログが重複しうる
                <li key={i}>
                  [{formatClock(entry.at)}] {Math.round(entry.progress * 100)}%
                  {entry.step && ` ${stepLabel(job.stage, entry.step)}`}
                  {entry.message && ` — ${entry.message}`}
                </li>
              ))}
            </ul>
          </details>
        )}

        {!isTerminal && (
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => onCancel(job.jobId)}
              className="rounded-md bg-gray-100 dark:bg-gray-800 px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400"
            >
              キャンセル
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
