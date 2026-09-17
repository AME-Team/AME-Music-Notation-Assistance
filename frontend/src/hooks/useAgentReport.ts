import { useQuery } from "@tanstack/react-query";
import type { AgentReportResponse } from "../api/client";
import { getAgentReport } from "../api/client";

export function agentReportKey(runId: string) {
  return ["agentReport", runId] as const;
}

/**
 * #47: エージェント run の成果報告 report.md を取得するフック(設計書§8.6, §11.3)。
 *
 * run 終了時、エージェントが残した「何を、なぜ変えたか」の報告を取得し、
 * UI (DiffPanel等) に表示する。
 */
export function useAgentReport(runId: string | null) {
  const query = useQuery({
    queryKey: runId ? agentReportKey(runId) : ["agentReport", "__none__"],
    queryFn: () => getAgentReport(runId as string),
    enabled: runId !== null && runId.length > 0,
    retry: false,
    // report.md が未生成(404等)の間は2秒間隔で再試行し、生成完了次第自動取得する。
    // 一度取得できればポーリングを停止する(#47 Gate2レビュー指摘対応)。
    refetchInterval: (q) => (q.state.data ? false : 2000),
  });

  return {
    report: query.data as AgentReportResponse | undefined,
    isLoading: query.isLoading,
    error: query.error as Error | null,
    refetch: query.refetch,
  };
}
