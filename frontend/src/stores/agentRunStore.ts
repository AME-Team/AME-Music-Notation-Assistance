import { create } from "zustand";
import { cancelAgentRun } from "../api/client";
import { type AgentEvent, subscribeAgentEvents } from "../lib/sse";

export type AgentRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "truncated";

interface TrackedAgentRun {
  runId: string;
  status: AgentRunStatus;
  events: AgentEvent[];
  error?: string;
}

interface AgentRunStoreState {
  runs: Record<string, TrackedAgentRun>;
  track: (runId: string) => void;
  cancel: (runId: string) => Promise<void>;
  dismiss: (runId: string) => void;
}

const unsubscribers = new Map<string, () => void>();
// `kind`ごとの終端イベントが運ぶ実ステータス(§11.3のAgentRunStatus)。
// "done"はpayload.statusが"completed"/"truncated"のいずれかを持つため、
// そちらを優先する(#49 claude.py::_terminal_event参照)。
const TERMINAL_STATUS: Partial<Record<AgentEvent["kind"], AgentRunStatus>> = {
  error: "failed",
  cancelled: "cancelled",
};

function statusForEvent(event: AgentEvent, fallback: AgentRunStatus): AgentRunStatus {
  if (event.kind === "done") {
    const payloadStatus = event.payload.status;
    if (payloadStatus === "completed" || payloadStatus === "truncated") return payloadStatus;
    return "completed";
  }
  return TERMINAL_STATUS[event.kind] ?? fallback;
}

/**
 * #51 AgentConsole向け。`jobStore`(#13)と同じ購読/破棄パターンだが、進捗率
 * ではなく`AgentEvent`の全履歴を保持する(§12.5「仮想スクロールとし、1 run で
 * 数千イベントに達しても劣化しないこと」の描画側の前提)。
 *
 * イベント配列は`push`で破壊的に追記する(#13の`jobStore`のように
 * `[...events, event]`で毎回複製すると、1 runあたり数千イベントで
 * O(イベント数^2)のコピーコストが積み上がるため)。Reactへは配列を
 * 保持するレコードそのものを毎回新しいオブジェクトとして`set`し、
 * 参照の変化で再描画をトリガーする。
 */
export const useAgentRunStore = create<AgentRunStoreState>((set, get) => ({
  runs: {},

  track: (runId: string) => {
    if (unsubscribers.has(runId)) return;
    const events: AgentEvent[] = [];
    set((state) => ({
      runs: { ...state.runs, [runId]: { runId, status: "running", events } },
    }));
    const unsubscribe = subscribeAgentEvents(
      runId,
      (event) => {
        events.push(event);
        const prev = get().runs[runId];
        const status = statusForEvent(event, prev?.status ?? "running");
        const error =
          event.kind === "done" || event.kind === "error"
            ? ((event.payload.error as string | undefined) ?? prev?.error)
            : prev?.error;
        set((state) => ({
          runs: { ...state.runs, [runId]: { runId, status, events, error } },
        }));
      },
      (error) => {
        console.error(`agent run ${runId} SSE error`, error);
      },
    );
    unsubscribers.set(runId, unsubscribe);
  },

  cancel: async (runId: string) => {
    await cancelAgentRun(runId);
  },

  dismiss: (runId: string) => {
    unsubscribers.get(runId)?.();
    unsubscribers.delete(runId);
    set((state) => {
      const rest = { ...state.runs };
      delete rest[runId];
      return { runs: rest };
    });
  },
}));
