import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { BeatmapEditRequest } from "../api/client";
import { getBeatmap, patchBeatmap } from "../api/client";

function beatmapKey(projectId: string) {
  return ["beatmap", projectId] as const;
}

/** #18: `null` はビート推定が未実行であることを表す(404 ではなくエラー扱いしない)。 */
export function useBeatmap(projectId: string) {
  return useQuery({
    queryKey: beatmapKey(projectId),
    queryFn: () => getBeatmap(projectId),
  });
}

/** #20 BeatGridEditor: 手動補正を送信し、成功したらキャッシュを更新する。 */
export function usePatchBeatmap(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<BeatmapEditRequest>) => patchBeatmap(projectId, body),
    onSuccess: (beatmap) => {
      queryClient.setQueryData(beatmapKey(projectId), beatmap);
      // #29-M2レビュー指摘: 実際に補正が適用されるとサーバ側(api/media.pyの
      // patch_beatmap)がquantizeのmeta.jsonを無効化しstale化する。このキーを
      // invalidateしないと、ProjectWorkspaceの「量子化結果が古い」バッジが
      // 補正直後には反映されず、利用者が気づかないまま古い結果をエクスポート
      // しうる。
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    },
  });
}
