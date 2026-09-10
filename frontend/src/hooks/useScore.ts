import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
 * 2. `onError`: スナップショットへロールバックする(422/409等でサーバが
 *    拒否した場合、プレビューを巻き戻す)
 * 3. `onSuccess`: サーバの権威ある応答(実際に採番されたノートID・再計算された
 *    onset_sec等を含む)でキャッシュを置き換える
 */
export function useApplyScoreOps(projectId: string) {
  const queryClient = useQueryClient();
  const key = scoreKey(projectId);
  return useMutation({
    mutationFn: (ops: NoteOp[]) => applyScoreOps(projectId, ops),
    onMutate: async (ops: NoteOp[]) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previousScore = queryClient.getQueryData<ScoreIR | null>(key);
      if (previousScore) {
        queryClient.setQueryData(key, applyOpsOptimistically(previousScore, ops));
      }
      return { previousScore };
    },
    onError: (_err, _ops, context) => {
      if (context && context.previousScore !== undefined) {
        queryClient.setQueryData(key, context.previousScore);
      }
    },
    onSuccess: (score) => {
      queryClient.setQueryData(key, score);
    },
  });
}
