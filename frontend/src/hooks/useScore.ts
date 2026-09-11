import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import type { NoteOp, ScoreIR, UndoRedoResult } from "../api/client";
import { applyScoreOps, getScore, redoScoreOps, undoScoreOps } from "../api/client";
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
 * #31/#32: ノート編集オペレーション・Undo・Redoをまとめて扱う。このリポジトリで
 * 初めての`onMutate`/ロールバック実装(既存の`usePatchBeatmap`は非楽観的)。
 *
 * 3つのmutation(`applyOps`/`undo`/`redo`)で**1つの`latestMutationIdRef`を
 * 共有**する(#32設計: #31-M3レビュー2巡目で「同一mutation内の応答順序逆転」
 * (例: 連続する2回のノート編集で古い方の応答が後着する)が指摘され
 * `mutationId`ガードで修正したが、`applyOps`実行直後に`undo`を叩くような
 * **別mutation間**の応答順序逆転も同じ理屈で起こりうる。3つのmutationで
 * カウンタを分けているとこれを検出できないため、最初から共有する)。
 *
 * - `applyOps`: `onMutate`で`applyOpsOptimistically`による簡易プレビューを
 *   即座に反映し、`onSuccess`でサーバの権威ある応答に置き換える。
 * - `undo`/`redo`: サーバ応答を待たずに変更する対象がない(どのノートが
 *   どう変わるかはサーバの`ops.jsonl`/`undo_state.json`にしか無い)ため、
 *   楽観的プレビューは行わずサーバ応答をそのまま反映する。
 * - いずれも`onError`(409等)では`invalidateQueries`でサーバの権威ある状態へ
 *   再同期する(#31-M3レビュー指摘: ロールバックだけでは直前スナップショット
 *   自体がサーバの最新状態とズレたままになり、同じ409を繰り返しうる)。
 * - `canUndo`/`canRedo`(ボタン活性化用)もこのフックが内部で保持し、必ず
 *   上記と同じ`mutationId`ガードを経由してから更新する(#32-M3レビュー指摘:
 *   呼び出し側`.mutate(vars, {onSuccess})`のコールバックはmutationIdガードを
 *   経由せず必ず実行されるため、そちらでUI状態を更新すると世代遅れの応答
 *   でもcanUndo/canRedoが書き換わってしまう。サーバに専用の状態取得
 *   エンドポイントは追加せず、`applied`フラグを使った自己修正的な設計とする)。
 */
export function useScoreEditing(projectId: string) {
  const queryClient = useQueryClient();
  const key = scoreKey(projectId);
  const latestMutationIdRef = useRef(0);
  const [canUndo, setCanUndo] = useState(true);
  const [canRedo, setCanRedo] = useState(true);

  const applyOps = useMutation({
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
      setCanUndo(true);
      setCanRedo(false);
    },
  });

  const undo = useMutation({
    mutationFn: () => undoScoreOps(projectId),
    onMutate: async () => {
      const mutationId = ++latestMutationIdRef.current;
      await queryClient.cancelQueries({ queryKey: key });
      return { mutationId };
    },
    onError: (_err, _vars, context) => {
      if (context?.mutationId !== latestMutationIdRef.current) return;
      queryClient.invalidateQueries({ queryKey: key });
    },
    onSuccess: (result: UndoRedoResult, _vars, context) => {
      if (context?.mutationId !== latestMutationIdRef.current) return;
      queryClient.setQueryData(key, result.score);
      setCanUndo(result.applied);
      if (result.applied) setCanRedo(true);
    },
  });

  const redo = useMutation({
    mutationFn: () => redoScoreOps(projectId),
    onMutate: async () => {
      const mutationId = ++latestMutationIdRef.current;
      await queryClient.cancelQueries({ queryKey: key });
      return { mutationId };
    },
    onError: (_err, _vars, context) => {
      if (context?.mutationId !== latestMutationIdRef.current) return;
      queryClient.invalidateQueries({ queryKey: key });
    },
    onSuccess: (result: UndoRedoResult, _vars, context) => {
      if (context?.mutationId !== latestMutationIdRef.current) return;
      queryClient.setQueryData(key, result.score);
      setCanRedo(result.applied);
      if (result.applied) setCanUndo(true);
    },
  });

  // #32-M3レビュー指摘: mutationIdガードはコールバックの適用可否しか制御せず、
  // リクエスト自体は並行して送出されうる。applyOps実行中にundo/redoを叩くと
  // (逆も同様)、サーバ側の楽観的並行性制御(score/undo_state.json両方を
  // 対象にした409判定、`api/score.py`参照)がデータ破損は防ぐものの、
  // 片方が無駄に409で失敗しUXが混乱する。いずれかのmutationが進行中の間は
  // undo/redoの新規発行自体を抑止する(ボタンのdisabled propだけでなく
  // キーボードショートカットの発火元もこの関数経由にすることで一元的に防ぐ)。
  const isMutating = applyOps.isPending || undo.isPending || redo.isPending;

  const triggerUndo = useCallback(() => {
    if (!canUndo || isMutating) return;
    undo.mutate();
  }, [canUndo, isMutating, undo.mutate]);

  const triggerRedo = useCallback(() => {
    if (!canRedo || isMutating) return;
    redo.mutate();
  }, [canRedo, isMutating, redo.mutate]);

  return { applyOps, undo, redo, canUndo, canRedo, isMutating, triggerUndo, triggerRedo };
}
