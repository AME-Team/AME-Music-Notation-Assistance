import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "../lib/sse";

const listeners: Record<string, (event: AgentEvent) => void> = {};

vi.mock("../lib/sse", () => ({
  subscribeAgentEvents: vi.fn((runId: string, onEvent: (event: AgentEvent) => void) => {
    listeners[runId] = onEvent;
    return vi.fn();
  }),
}));

vi.mock("../api/client", () => ({
  cancelAgentRun: vi.fn().mockResolvedValue(undefined),
}));

function makeEvent(overrides: Partial<AgentEvent>): AgentEvent {
  return {
    run_id: "run_1",
    seq: 0,
    kind: "thinking",
    tool_name: null,
    payload: {},
    ...overrides,
  };
}

describe("useAgentRunStore", () => {
  beforeEach(() => {
    for (const key of Object.keys(listeners)) delete listeners[key];
    vi.resetModules();
    vi.clearAllMocks();
  });

  it("tracks a run and appends events as they arrive", async () => {
    const { useAgentRunStore } = await import("./agentRunStore");
    useAgentRunStore.getState().track("run_1");

    expect(useAgentRunStore.getState().runs.run_1).toMatchObject({ status: "running", events: [] });

    listeners.run_1(makeEvent({ seq: 0, kind: "thinking", payload: { text: "..." } }));
    listeners.run_1(makeEvent({ seq: 1, kind: "tool_use", tool_name: "score_query" }));

    expect(useAgentRunStore.getState().runs.run_1.events).toHaveLength(2);
    expect(useAgentRunStore.getState().runs.run_1.status).toBe("running");
  });

  it("derives 'completed' status from a done event's payload.status", async () => {
    const { useAgentRunStore } = await import("./agentRunStore");
    useAgentRunStore.getState().track("run_1");

    listeners.run_1(
      makeEvent({ seq: 0, kind: "done", payload: { status: "completed", staged_ops_count: 1 } }),
    );

    expect(useAgentRunStore.getState().runs.run_1.status).toBe("completed");
  });

  it("derives 'truncated' status from a done event when budget/turns were exhausted", async () => {
    const { useAgentRunStore } = await import("./agentRunStore");
    useAgentRunStore.getState().track("run_1");

    listeners.run_1(makeEvent({ seq: 0, kind: "done", payload: { status: "truncated" } }));

    expect(useAgentRunStore.getState().runs.run_1.status).toBe("truncated");
  });

  it("derives 'failed' status and captures the error message from an error event", async () => {
    const { useAgentRunStore } = await import("./agentRunStore");
    useAgentRunStore.getState().track("run_1");

    listeners.run_1(
      makeEvent({ seq: 0, kind: "error", payload: { status: "failed", error: "boom" } }),
    );

    expect(useAgentRunStore.getState().runs.run_1).toMatchObject({
      status: "failed",
      error: "boom",
    });
  });

  it("derives 'cancelled' status from a cancelled event", async () => {
    const { useAgentRunStore } = await import("./agentRunStore");
    useAgentRunStore.getState().track("run_1");

    listeners.run_1(makeEvent({ seq: 0, kind: "cancelled", payload: { message: "cancelled" } }));

    expect(useAgentRunStore.getState().runs.run_1.status).toBe("cancelled");
  });

  it("removes a run from state on dismiss", async () => {
    const { useAgentRunStore } = await import("./agentRunStore");
    useAgentRunStore.getState().track("run_2");
    expect(useAgentRunStore.getState().runs.run_2).toBeDefined();

    useAgentRunStore.getState().dismiss("run_2");
    expect(useAgentRunStore.getState().runs.run_2).toBeUndefined();
  });

  it("does not resubscribe if a run is already tracked", async () => {
    const sse = await import("../lib/sse");
    const { useAgentRunStore } = await import("./agentRunStore");

    useAgentRunStore.getState().track("run_3");
    useAgentRunStore.getState().track("run_3");

    expect(sse.subscribeAgentEvents).toHaveBeenCalledTimes(1);
  });

  it("calls cancelAgentRun via the store's cancel action", async () => {
    const client = await import("../api/client");
    const { useAgentRunStore } = await import("./agentRunStore");

    await useAgentRunStore.getState().cancel("run_4");

    expect(client.cancelAgentRun).toHaveBeenCalledWith("run_4");
  });
});
