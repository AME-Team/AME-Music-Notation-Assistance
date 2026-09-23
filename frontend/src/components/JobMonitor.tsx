import { useState } from "react";
import { stageLabel, stepLabel } from "../lib/jobSteps";
import { useJobStore } from "../stores/jobStore";
import { JobDetailModal } from "./JobDetailModal";

const STATUS_LABEL: Record<string, string> = {
  queued: "待機中",
  running: "実行中",
  succeeded: "完了",
  failed: "失敗",
  cancelled: "キャンセル済み",
};

/**
 * #13/§12.3: DSPジョブ進捗のトースト表示とキャンセル。
 *
 * UI刷新: 完了/キャンセルは`jobStore`側で既定5秒後に自動消去される
 * (トーストにマウスが乗っている間は`pin`でタイマーを止める)。失敗は
 * 見落とし防止のため自動では消さない。各トーストから[詳細]で
 * `JobDetailModal`(工程チェックリスト・ログ・エラー詳細)を開ける。
 */
export function JobMonitor() {
  const jobs = useJobStore((s) => s.jobs);
  const cancel = useJobStore((s) => s.cancel);
  const dismiss = useJobStore((s) => s.dismiss);
  const pin = useJobStore((s) => s.pin);
  const unpin = useJobStore((s) => s.unpin);
  const [detailJobId, setDetailJobId] = useState<string | null>(null);

  const entries = Object.values(jobs);
  const detailJob = detailJobId ? (jobs[detailJobId] ?? null) : null;

  if (entries.length === 0 && !detailJob) return null;

  return (
    <>
      <div className="fixed bottom-4 right-4 flex flex-col gap-2">
        {entries.map((job) => {
          const isTerminal = ["succeeded", "failed", "cancelled"].includes(job.status);
          const currentStepLabel = stepLabel(job.stage, job.step);
          return (
            // biome-ignore lint/a11y/noStaticElementInteractions: ホバー中は自動消去タイマーを止めるだけの検出領域で、新たな対話的意味(role)は持たせない。
            <div
              key={job.jobId}
              onMouseEnter={() => pin(job.jobId)}
              onMouseLeave={() => unpin(job.jobId)}
              className="w-72 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-3 shadow-sm transition-opacity duration-200 ease-out"
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {stageLabel(job.stage)}: {STATUS_LABEL[job.status] ?? job.status}
                </span>
                {isTerminal ? (
                  <button
                    type="button"
                    onClick={() => dismiss(job.jobId)}
                    className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300"
                  >
                    ×
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void cancel(job.jobId)}
                    className="text-xs text-red-500 hover:text-red-700 dark:hover:text-red-300"
                  >
                    キャンセル
                  </button>
                )}
              </div>
              <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                <div
                  className={`h-full rounded-full transition-all duration-150 ease-out ${
                    job.status === "failed" ? "bg-red-500" : "bg-blue-500"
                  }`}
                  style={{ width: `${Math.round(job.progress * 100)}%` }}
                />
              </div>
              <div className="mt-1 flex items-center justify-between gap-2">
                <p className="truncate text-xs text-gray-500 dark:text-gray-400">
                  {currentStepLabel ?? job.message ?? ""}
                </p>
                <button
                  type="button"
                  onClick={() => {
                    // Gate2レビュー指摘(2巡目・MIDDLE): モーダルへマウスを
                    // 移すとトースト側のonMouseLeaveでunpinされ、詳細を
                    // 読んでいる途中で自動消去タイマーが動いてモーダルが
                    // 勝手に閉じていた。詳細表示中は明示的にpinし続ける。
                    pin(job.jobId);
                    setDetailJobId(job.jobId);
                  }}
                  className="shrink-0 text-xs text-blue-600 dark:text-blue-400 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
                >
                  詳細
                </button>
              </div>
            </div>
          );
        })}
      </div>
      <JobDetailModal
        job={detailJob}
        onClose={() => {
          if (detailJobId) unpin(detailJobId);
          setDetailJobId(null);
        }}
        onCancel={(jobId) => void cancel(jobId)}
      />
    </>
  );
}
