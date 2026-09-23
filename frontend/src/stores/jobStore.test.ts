import { beforeEach, describe, expect, it, vi } from "vitest";
import type { JobProgressEvent } from "../lib/sse";

const listeners: Record<string, (event: JobProgressEvent) => void> = {};

vi.mock("../lib/sse", () => ({
  subscribeJobEvents: vi.fn((jobId: string, onEvent: (event: JobProgressEvent) => void) => {
    listeners[jobId] = onEvent;
    return vi.fn();
  }),
}));

vi.mock("../api/client", () => ({
  cancelJob: vi.fn().mockResolvedValue(undefined),
}));

describe("useJobStore", () => {
  beforeEach(() => {
    for (const key of Object.keys(listeners)) delete listeners[key];
    vi.resetModules();
    vi.clearAllMocks();
  });

  it("tracks a job and updates progress as events arrive", async () => {
    const { useJobStore } = await import("./jobStore");
    useJobStore.getState().track("job_1");

    expect(useJobStore.getState().jobs.job_1).toMatchObject({ status: "queued", progress: 0 });

    listeners.job_1({ job_id: "job_1", status: "running", progress: 0.5 });
    expect(useJobStore.getState().jobs.job_1).toMatchObject({ status: "running", progress: 0.5 });

    listeners.job_1({ job_id: "job_1", status: "succeeded", progress: 1.0 });
    expect(useJobStore.getState().jobs.job_1).toMatchObject({ status: "succeeded", progress: 1.0 });
  });

  it("removes a job from state on dismiss", async () => {
    const { useJobStore } = await import("./jobStore");
    useJobStore.getState().track("job_2");
    expect(useJobStore.getState().jobs.job_2).toBeDefined();

    useJobStore.getState().dismiss("job_2");
    expect(useJobStore.getState().jobs.job_2).toBeUndefined();
  });

  it("does not resubscribe if a job is already tracked", async () => {
    const sse = await import("../lib/sse");
    const { useJobStore } = await import("./jobStore");

    useJobStore.getState().track("job_3");
    useJobStore.getState().track("job_3");

    expect(sse.subscribeJobEvents).toHaveBeenCalledTimes(1);
  });

  it("succeeded/cancelledのジョブは5秒後に自動で消える", async () => {
    vi.useFakeTimers();
    try {
      const { useJobStore } = await import("./jobStore");
      useJobStore.getState().track("job_4");
      listeners.job_4({ job_id: "job_4", status: "succeeded", progress: 1.0 });
      expect(useJobStore.getState().jobs.job_4).toBeDefined();

      vi.advanceTimersByTime(4999);
      expect(useJobStore.getState().jobs.job_4).toBeDefined();

      vi.advanceTimersByTime(1);
      expect(useJobStore.getState().jobs.job_4).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("failedのジョブは自動で消えず、手動でdismissするまで残る", async () => {
    vi.useFakeTimers();
    try {
      const { useJobStore } = await import("./jobStore");
      useJobStore.getState().track("job_5");
      listeners.job_5({ job_id: "job_5", status: "failed", progress: 0.5, message: "boom" });

      vi.advanceTimersByTime(60_000);
      expect(useJobStore.getState().jobs.job_5).toMatchObject({ status: "failed" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("pin中は自動消去タイマーを止め、unpinで再開する", async () => {
    vi.useFakeTimers();
    try {
      const { useJobStore } = await import("./jobStore");
      useJobStore.getState().track("job_6");
      listeners.job_6({ job_id: "job_6", status: "succeeded", progress: 1.0 });

      vi.advanceTimersByTime(2000);
      useJobStore.getState().pin("job_6");
      vi.advanceTimersByTime(10_000);
      expect(useJobStore.getState().jobs.job_6).toBeDefined();

      useJobStore.getState().unpin("job_6");
      vi.advanceTimersByTime(5000);
      expect(useJobStore.getState().jobs.job_6).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("イベント履歴を蓄積する(詳細モーダル用)", async () => {
    const { useJobStore } = await import("./jobStore");
    useJobStore.getState().track("job_7");
    listeners.job_7({
      job_id: "job_7",
      status: "running",
      progress: 0.2,
      step: "piano",
      message: "transcribing piano",
    });
    listeners.job_7({
      job_id: "job_7",
      status: "running",
      progress: 0.5,
      step: "bass",
      message: "transcribing bass",
    });

    expect(useJobStore.getState().jobs.job_7.events).toHaveLength(2);
    expect(useJobStore.getState().jobs.job_7.events[0]).toMatchObject({
      step: "piano",
      progress: 0.2,
    });
  });
});
