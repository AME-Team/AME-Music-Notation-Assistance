import { create } from "zustand";
import { cancelJob } from "../api/client";
import { type JobProgressEvent, subscribeJobEvents } from "../lib/sse";

/** UI刷新: 完了トーストを自動で消すまでの待ち時間。 */
const AUTO_DISMISS_MS = 5000;

export interface JobEventLogEntry {
  at: number;
  step?: string;
  progress: number;
  message?: string;
}

export interface TrackedJob {
  jobId: string;
  /** どの工程(separate/beat/transcribe/quantize/dummy)かの機械可読キー。 */
  stage?: string;
  /** 画面表示用のラベル(例:「採譜」)。呼び出し元が渡さなければ`stage`から導出する。 */
  label?: string;
  status: JobProgressEvent["status"] | "queued";
  progress: number;
  message?: string;
  step?: string;
  stepIndex?: number;
  stepTotal?: number;
  startedAt: number;
  finishedAt?: number;
  /** 詳細モーダル用の履歴ログ(時刻付き)。 */
  events: JobEventLogEntry[];
  /**
   * トーストにマウスが乗っている間は自動消去タイマーを止める(#UI刷新)。
   * ユーザーが読んでいる最中にトーストが消えてしまう体験を避けるため。
   */
  pinned: boolean;
}

interface TrackMeta {
  stage?: string;
  label?: string;
}

interface JobStoreState {
  jobs: Record<string, TrackedJob>;
  track: (jobId: string, meta?: TrackMeta) => void;
  cancel: (jobId: string) => Promise<void>;
  dismiss: (jobId: string) => void;
  pin: (jobId: string) => void;
  unpin: (jobId: string) => void;
}

const unsubscribers = new Map<string, () => void>();
const dismissTimers = new Map<string, ReturnType<typeof setTimeout>>();

function clearDismissTimer(jobId: string): void {
  const timer = dismissTimers.get(jobId);
  if (timer) {
    clearTimeout(timer);
    dismissTimers.delete(jobId);
  }
}

const TERMINAL_AUTO_DISMISS_STATUSES = new Set<JobProgressEvent["status"]>([
  "succeeded",
  "cancelled",
]);

export const useJobStore = create<JobStoreState>((set, get) => {
  function scheduleAutoDismissIfEligible(jobId: string): void {
    const job = get().jobs[jobId];
    if (!job || job.pinned) return;
    if (!TERMINAL_AUTO_DISMISS_STATUSES.has(job.status as JobProgressEvent["status"])) return;
    clearDismissTimer(jobId);
    const timer = setTimeout(() => get().dismiss(jobId), AUTO_DISMISS_MS);
    dismissTimers.set(jobId, timer);
  }

  return {
    jobs: {},

    track: (jobId: string, meta?: TrackMeta) => {
      if (unsubscribers.has(jobId)) return;
      const now = Date.now();
      set((state) => ({
        jobs: {
          ...state.jobs,
          [jobId]: {
            jobId,
            stage: meta?.stage,
            label: meta?.label,
            status: "queued",
            progress: 0,
            startedAt: now,
            events: [],
            pinned: false,
          },
        },
      }));
      const unsubscribe = subscribeJobEvents(
        jobId,
        (event) => {
          const at = Date.now();
          set((state) => {
            const current = state.jobs[jobId];
            const isTerminal =
              event.status === "succeeded" ||
              event.status === "failed" ||
              event.status === "cancelled";
            return {
              jobs: {
                ...state.jobs,
                [jobId]: {
                  ...current,
                  jobId,
                  status: event.status,
                  progress: event.progress ?? current?.progress ?? 0,
                  message: event.message,
                  step: event.step,
                  stepIndex: event.step_index,
                  stepTotal: event.step_total,
                  startedAt: current?.startedAt ?? at,
                  finishedAt: isTerminal ? at : current?.finishedAt,
                  events: [
                    ...(current?.events ?? []),
                    {
                      at,
                      step: event.step,
                      progress: event.progress ?? current?.progress ?? 0,
                      message: event.message,
                    },
                  ],
                  pinned: current?.pinned ?? false,
                },
              },
            };
          });
          // UI刷新: 完了/キャンセルは既定で数秒後に自動で消す。失敗は見落とし
          // 防止のため自動では消さず、ユーザーが手動で閉じるまで残す。
          if (event.status === "succeeded" || event.status === "cancelled") {
            scheduleAutoDismissIfEligible(jobId);
          }
        },
        (error) => {
          console.error(`job ${jobId} SSE error`, error);
        },
      );
      unsubscribers.set(jobId, unsubscribe);
    },

    cancel: async (jobId: string) => {
      await cancelJob(jobId);
    },

    dismiss: (jobId: string) => {
      clearDismissTimer(jobId);
      unsubscribers.get(jobId)?.();
      unsubscribers.delete(jobId);
      set((state) => {
        const rest = { ...state.jobs };
        delete rest[jobId];
        return { jobs: rest };
      });
    },

    pin: (jobId: string) => {
      clearDismissTimer(jobId);
      set((state) => {
        const job = state.jobs[jobId];
        if (!job) return state;
        return { jobs: { ...state.jobs, [jobId]: { ...job, pinned: true } } };
      });
    },

    unpin: (jobId: string) => {
      set((state) => {
        const job = state.jobs[jobId];
        if (!job) return state;
        return { jobs: { ...state.jobs, [jobId]: { ...job, pinned: false } } };
      });
      scheduleAutoDismissIfEligible(jobId);
    },
  };
});
