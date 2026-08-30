import { create } from "zustand";
import { cancelJob } from "../api/client";
import { type JobProgressEvent, subscribeJobEvents } from "../lib/sse";

interface TrackedJob {
  jobId: string;
  status: JobProgressEvent["status"] | "queued";
  progress: number;
  message?: string;
}

interface JobStoreState {
  jobs: Record<string, TrackedJob>;
  track: (jobId: string) => void;
  cancel: (jobId: string) => Promise<void>;
  dismiss: (jobId: string) => void;
}

const unsubscribers = new Map<string, () => void>();

export const useJobStore = create<JobStoreState>((set) => ({
  jobs: {},

  track: (jobId: string) => {
    if (unsubscribers.has(jobId)) return;
    set((state) => ({
      jobs: { ...state.jobs, [jobId]: { jobId, status: "queued", progress: 0 } },
    }));
    const unsubscribe = subscribeJobEvents(
      jobId,
      (event) => {
        set((state) => ({
          jobs: {
            ...state.jobs,
            [jobId]: {
              jobId,
              status: event.status,
              progress: event.progress ?? state.jobs[jobId]?.progress ?? 0,
              message: event.message,
            },
          },
        }));
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
    unsubscribers.get(jobId)?.();
    unsubscribers.delete(jobId);
    set((state) => {
      const rest = { ...state.jobs };
      delete rest[jobId];
      return { jobs: rest };
    });
  },
}));
