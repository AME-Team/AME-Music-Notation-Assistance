import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { DiffResponse, RejectResponse, ScopeRequest } from "../api/client";
import { acceptDiff, getDiff, rejectDiff } from "../api/client";
import { scoreKey } from "./useScore";

function diffKey(projectId: string, runId: string) {
  return ["diff", projectId, runId] as const;
}

/**
 * #41: DiffPanel向け。L1整音1回分(`runId`)のL0 vs AI提案の差分を取得し、
 * 承認/却下ミューテーションを提供する。
 *
 * `useScoreEditing`(#31/#32)と異なり楽観更新は行わない: 差分の中身(どのノートが
 * どう変わるか)はサーバの`current.json`/`score/staging/{run_id}.json`の突き合わせ
 * でしか分からず、クライアント側で先読みできないため。
 *
 * 承認成功時は`remaining_diff`でこのフックのキャッシュを更新すると同時に、
 * 返ってきた`score`で`useScore`のキャッシュも更新する(ピアノロール/楽譜プレビューの
 * 即時反映、#41完了条件)。却下は`current.json`を変更しないため`useScore`側は
 * 更新しない。
 */
export function useDiff(projectId: string, runId: string | null) {
  const queryClient = useQueryClient();
  const key = runId ? diffKey(projectId, runId) : null;

  const diffQuery = useQuery({
    queryKey: key ?? diffKey(projectId, "__none__"),
    queryFn: () => getDiff(projectId, runId as string),
    enabled: runId !== null,
  });

  const accept = useMutation({
    mutationFn: (scope: ScopeRequest) => acceptDiff(projectId, runId as string, scope),
    onSuccess: (result) => {
      if (!key) return;
      queryClient.setQueryData(key, result.remaining_diff);
      queryClient.setQueryData(scoreKey(projectId), result.score);
    },
  });

  const reject = useMutation({
    mutationFn: (scope: ScopeRequest) => rejectDiff(projectId, runId as string, scope),
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
