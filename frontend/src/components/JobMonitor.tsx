import { useJobStore } from "../stores/jobStore";

const STATUS_LABEL: Record<string, string> = {
  queued: "待機中",
  running: "実行中",
  succeeded: "完了",
  failed: "失敗",
  cancelled: "キャンセル済み",
};

/** #13/§12.3: DSPジョブ進捗のトースト表示とキャンセル。 */
export function JobMonitor() {
  const jobs = useJobStore((s) => s.jobs);
  const cancel = useJobStore((s) => s.cancel);
  const dismiss = useJobStore((s) => s.dismiss);

  const entries = Object.values(jobs);
  if (entries.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-2">
      {entries.map((job) => {
        const isTerminal = ["succeeded", "failed", "cancelled"].includes(job.status);
        return (
          <div
            key={job.jobId}
            className="w-72 rounded-lg border border-gray-200 bg-white p-3 shadow-lg"
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-900">
                {STATUS_LABEL[job.status] ?? job.status}
              </span>
              {isTerminal ? (
                <button
                  type="button"
                  onClick={() => dismiss(job.jobId)}
                  className="text-xs text-gray-400 hover:text-gray-600"
                >
                  ×
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void cancel(job.jobId)}
                  className="text-xs text-red-500 hover:text-red-700"
                >
                  キャンセル
                </button>
              )}
            </div>
            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
              <div
                className={`h-full rounded-full transition-all ${
                  job.status === "failed" ? "bg-red-500" : "bg-blue-500"
                }`}
                style={{ width: `${Math.round(job.progress * 100)}%` }}
              />
            </div>
            {job.message && <p className="mt-1 text-xs text-gray-500">{job.message}</p>}
          </div>
        );
      })}
    </div>
  );
}
