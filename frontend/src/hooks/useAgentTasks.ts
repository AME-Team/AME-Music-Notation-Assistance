import { useQuery } from "@tanstack/react-query";
import type { AgentProviderInfo, TaskDefinitionResponse } from "../api/client";
import { listAgentProviders, listAgentTasks } from "../api/client";

/** #51 AgentTaskLauncher: 標準タスク定義一覧(§8.7)。静的データのため常時有効。 */
export function useAgentTasks() {
  const query = useQuery({
    queryKey: ["agentTasks"],
    queryFn: listAgentTasks,
    staleTime: Number.POSITIVE_INFINITY,
  });
  return {
    tasks: (query.data ?? []) as TaskDefinitionResponse[],
    isLoading: query.isLoading,
    error: query.error as Error | null,
  };
}

/** #51 AgentTaskLauncher: プロバイダ一覧(#42) — `configured`で選択肢を絞る。 */
export function useAgentProviders() {
  const query = useQuery({
    queryKey: ["agentProviders"],
    queryFn: listAgentProviders,
    staleTime: Number.POSITIVE_INFINITY,
  });
  return {
    providers: (query.data ?? []) as AgentProviderInfo[],
    isLoading: query.isLoading,
    error: query.error as Error | null,
  };
}
