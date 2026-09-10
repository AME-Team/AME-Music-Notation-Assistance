import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import type { NoteOp, ScoreIR } from "../api/client";
import { applyScoreOps, getScore } from "../api/client";
import { applyOpsOptimistically } from "../lib/scoreOps";

function scoreKey(projectId: string) {
  return ["score", projectId] as const;
}

/** #23: Score IR全体。`null`は採譜(transcribe)未実行(404を例外にしない、useBeatmapと同じ方針)。 */
export function useScore(projectId: string) {
  return useQuery({
    queryKey: scoreKey(projectId),
    queryFn: () => getScore(projectId),
  });
}

/**
 * #31: ノート編集オペレーションを楽観更新で適用する。このリポジトリで初めての
 * `onMutate`/ロールバック実装(既存の`usePatchBeatmap`は非楽観的)。
 *
 * 1. `onMutate`: 進行中の同キー取得をキャンセルし、現在のキャッシュをスナップ
 *    ショットしてから`applyOpsOptimistically`(サーバの応答を待たない簡易
 *    プレビュー、`lib/scoreOps.ts`参照)を即座に反映する
 * 2. `onError`: スナップショットへロールバックした上で、キャッシュを
 *    invalidateして再取得する(#31-M3レビュー指摘: 409は「クライアントの
 *    直前スナップショット自体がサーバの最新状態とズレている」ことを意味する
 *    ため、ロールバックだけでは同じ409を繰り返しうる。再取得でサーバの
 *    権威ある状態へ再同期する)
 * 3. `onSuccess`: サーバの権威ある応答(実際に採番されたノートID・再計算された
 *    onset_sec等を含む)でキャッシュを置き換える。ただし複数の編集opが
 *    同時に飛んでいる場合、先に発行したmutationの応答が後から解決する
 *    (応答順序の逆転)ことがある(#31-M3レビュー指摘)。`mutationId`で
 *    「自分が最後に発行されたmutationか」を判定し、そうでなければ
 *    setQueryData/invalidateのどちらも行わない(#31-M3レビュー指摘、
 *    2巡目: invalidateすら非最新側で行うと、そのバックグラウンド再取得が
 *    最新mutationのonSuccessより後に解決してキャッシュを巻き戻すレースが
 *    別経路で再発しうる。最新でないmutationの結果は完全に無視し、最新
 *    mutation自身のonSuccess/onErrorにのみキャッシュ更新を委ねる)。
 */
export function useApplyScoreOps(projectId: string) {
  const queryClient = useQueryClient();
  const key = scoreKey(projectId);
  const latestMutationIdRef = useRef(0);
  return useMutation({
    mutationFn: (ops: NoteOp[]) => applyScoreOps(projectId, ops),
    onMutate: async (ops: NoteOp[]) => {
      const mutationId = ++latestMutationIdRef.current;
      await queryClient.cancelQueries({ queryKey: key });
      const previousScore = queryClient.getQueryData<ScoreIR | null>(key);
      if (previousScore) {
        queryClient.setQueryData(key, applyOpsOptimistically(previousScore, ops));
      }
      return { previousScore, mutationId };
    },
    onError: (_err, _ops, context) => {
      if (context?.mutationId !== latestMutationIdRef.current) return;
      if (context.previousScore !== undefined) {
        queryClient.setQueryData(key, context.previousScore);
      }
      queryClient.invalidateQueries({ queryKey: key });
    },
    onSuccess: (score, _ops, context) => {
      if (context?.mutationId !== latestMutationIdRef.current) return;
      queryClient.setQueryData(key, score);
    },
  });
}
