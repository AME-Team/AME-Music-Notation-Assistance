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
});
