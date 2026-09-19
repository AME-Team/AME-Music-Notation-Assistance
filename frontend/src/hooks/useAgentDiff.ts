import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { DiffResponse, RejectResponse, ScopeRequest } from "../api/client";
import { acceptAgentDiff, getAgentDiff, rejectAgentDiff } from "../api/client";
import { scoreKey } from "./useScore";

function agentDiffKey(runId: string) {
  return ["agentDiff", runId] as const;
}

/**
 * #51 DiffPanel向け。`useDiff`(#41)のL2版 — `/api/agent/runs/{run_id}/diff`系
 * (#46)を叩く点のみが異なる(URLがプロジェクトスコープではなくrunスコープ)。
 * 承認成功時に`useScore`のキャッシュを更新する挙動は`useDiff`と同じ。
 */
export function useAgentDiff(projectId: string, runId: string | null) {
  const queryClient = useQueryClient();
  const key = runId ? agentDiffKey(runId) : null;

  const diffQuery = useQuery({
    queryKey: key ?? agentDiffKey("__none__"),
    queryFn: () => getAgentDiff(runId as string),
    enabled: runId !== null,
  });

  const accept = useMutation({
    mutationFn: (scope: ScopeRequest) => acceptAgentDiff(runId as string, scope),
    onSuccess: (result) => {
      if (!key) return;
      queryClient.setQueryData(key, result.remaining_diff);
      queryClient.setQueryData(scoreKey(projectId), result.score);
    },
  });

  const reject = useMutation({
    mutationFn: (scope: ScopeRequest) => rejectAgentDiff(runId as string, scope),
    onSuccess: (result: RejectResponse) => {
      if (!key) return;
      queryClient.setQueryData(key, result.remaining_diff);
    },
  });

  return {
    diff: diffQuery.data as DiffResponse | undefined,
    isLoading: diffQuery.isLoading,
    error: diffQuery.error as Error | null,
    accept,
    reject,
  };
}
