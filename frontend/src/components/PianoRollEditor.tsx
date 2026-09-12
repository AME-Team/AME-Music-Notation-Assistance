import { useEffect, useState } from "react";
import type { NoteOp } from "../api/client";
import { useScore, useScoreEditing } from "../hooks/useScore";
import type { PianoRollNote } from "../lib/pianoRoll";
import { Inspector } from "./Inspector";
import { PianoRoll } from "./PianoRoll";
import { ScorePreview } from "./ScorePreview";

interface PianoRollEditorProps {
  projectId: string;
}

const BUTTON_CLASS =
  "rounded-md px-3 py-1.5 text-sm font-medium text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50";

/** #35: レイヤ表示切替(設計書§12.4)の3カテゴリ。`baseline`/`llm`/`agent`
 * (L0/L1/L2)は「AI提案」としてまとめる(design docの3分類に合わせる、
 * 5色の出自エンコーディングとは別軸のグルーピング)。
 */
type LayerCategory = "amt" | "ai" | "user";

const LAYER_LABEL: Record<LayerCategory, string> = {
  amt: "AMT原案",
  ai: "AI提案",
  user: "手動編集",
};

function layerCategoryForProvenance(provenance: string): LayerCategory {
  if (provenance === "amt") return "amt";
  if (provenance === "user") return "user";
  return "ai"; // baseline/llm/agent
}

/**
 * #30/#31/#32/#35: PianoRoll描画+編集操作のコンテナ。ツールバー(削除/分割/
 * 結合/±1オクターブ一括/元に戻す/やり直す/レイヤ表示切替)を結線する。
 * Score IR(採譜=transcribe未実行ならnull)が無ければ何も表示しない
 * (呼び出し元`ProjectWorkspace`は常にマウントしてよい)。
 *
 * ピアノロール編集UI自体はM2で意図的に見送られていた(ユーザー決定済み、M3の
 * スコープ)。楽譜プレビュー(#33)・再生同期(#34、`TransportBar`は
 * `ProjectWorkspace`が別途マウントする)・視覚エンコーディング/Inspector
 * (#35)は実装済み。
 */
export function PianoRollEditor({ projectId }: PianoRollEditorProps) {
  const { data: score, isLoading, error } = useScore(projectId);
  const { applyOps, canUndo, canRedo, isMutating, triggerUndo, triggerRedo } =
    useScoreEditing(projectId);
  const [selectedNoteIds, setSelectedNoteIds] = useState<Set<number>>(new Set());
  // #35: レイヤ表示切替(初期値は全カテゴリ表示)。`PianoRollEditor`自体が
  // 呼び出し元`ProjectWorkspace`から`key={projectId}`で再マウントされるため、
  // プロジェクト切替時のリセットは自然に賄われる(新規storeは不要)。
  const [visibleLayers, setVisibleLayers] = useState<Set<LayerCategory>>(
    () => new Set<LayerCategory>(["amt", "ai", "user"]),
  );

  function toggleLayer(category: LayerCategory) {
    setVisibleLayers((prev) => {
      const next = new Set(prev);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      return next;
    });
  }

  // #32: Ctrl+Z(Undo)・Ctrl+Shift+Z/Ctrl+Y(Redo)。BeatGridEditor等の数値入力
  // フォーカス中はネイティブUndoを奪わないよう何もしない。`PianoRollEditor`は
  // 常にマウントされる(採譜未実行時もこのeffect自体は登録される)ため、
  // `score`が無い(採譜未実行/読込中)間はリスナー自体を登録しない
  // (#32-M3レビュー指摘: 登録したままだとundo/redoがサーバへPOSTされてしまう)。
  // `triggerUndo`/`triggerRedo`(`useScoreEditing`)経由にすることで、
  // ボタンのdisabledと同じcanUndo/canRedo・他mutation進行中ガードを
  // キーボード操作にも一元的に適用する(#32-M3レビュー指摘: 直接
  // `undo.mutate()`を呼ぶとこのガードを迂回してしまう)。このプロジェクトは
  // Windows専用(設計書追補#82)のためCmd(metaKey)には対応しない。
  useEffect(() => {
    if (!score) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (!e.ctrlKey) return;
      const key = e.key.toLowerCase();
      if (key === "z" && !e.shiftKey) {
        e.preventDefault();
        triggerUndo();
      } else if ((key === "z" && e.shiftKey) || key === "y") {
        e.preventDefault();
        triggerRedo();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [score, triggerUndo, triggerRedo]);

  if (isLoading) return null;
  if (error) {
    return (
      <section className="space-y-2 rounded-lg border border-gray-200 p-4">
        <h3 className="text-lg font-semibold text-gray-700">ピアノロール</h3>
        <p className="text-sm text-red-600">{(error as Error).message}</p>
      </section>
    );
  }
  if (!score) return null; // 採譜(transcribe)未実行。

  // #30-M3レビュー指摘: 現状は先頭パートのみ編集対象とする(意図的なスコープ
  // 限定)。複数パート(連弾・オーケストラ譜等)のパート選択UIはM3後続PRの
  // 課題とする。
  const part = score.parts[0];
  if (!part) return null;

  // #31: 編集(tick空間での描画/操作)は量子化済み(onset_tick/duration_tick
  // 設定済み)のノートのみ対象にする。量子化前のノートはtick位置を持たない。
  const editableNotes: PianoRollNote[] = part.notes
    .filter(
      (n): n is typeof n & { onset_tick: number; duration_tick: number } =>
        n.onset_tick !== null && n.duration_tick !== null,
    )
    .map((n) => ({
      id: n.id,
      onset_tick: n.onset_tick,
      duration_tick: n.duration_tick,
      midi: n.midi,
      status: n.status,
      provenance: n.provenance,
      flags: n.flags,
    }));

  // #35: レイヤ表示切替は表示のみに影響させる(`editableNotes`自体は
  // split/merge等が選択ノートを引くために使うため、非表示レイヤーの
  // ノートが選択済みのまま残っていても正しく引けるようにフィルタしない)。
  const visibleNotes = editableNotes.filter((n) =>
    visibleLayers.has(layerCategoryForProvenance(n.provenance)),
  );

  const selectedArray = [...selectedNoteIds];
  const canDelete = selectedArray.length >= 1;
  const canSplit = selectedArray.length === 1;
  const canMerge = selectedArray.length >= 2;
  // #35: Inspectorは単一選択時のみ表示する。フルの`ScoreNote`(velocity/
  // provenance/ai_reason等、`PianoRollNote`には無いフィールド)が要るため
  // `editableNotes`ではなく元の`part.notes`から引く。
  const inspectedNote =
    selectedArray.length === 1 ? (part.notes.find((n) => n.id === selectedArray[0]) ?? null) : null;

  function handleApplyOps(ops: NoteOp[]) {
    applyOps.mutate(ops);
  }

  function handleDelete() {
    if (selectedArray.length === 0) return;
    handleApplyOps([{ type: "note.delete", note_ids: selectedArray }]);
    setSelectedNoteIds(new Set());
  }

  function handleSplit() {
    if (selectedArray.length !== 1) return;
    const note = editableNotes.find((n) => n.id === selectedArray[0]);
    if (!note) return;
    const atTick = note.onset_tick + Math.floor(note.duration_tick / 2);
    if (atTick <= note.onset_tick || atTick >= note.onset_tick + note.duration_tick) return;
    handleApplyOps([{ type: "note.split", note_id: note.id, at_tick: atTick }]);
  }

  function handleMerge() {
    if (selectedArray.length < 2) return;
    handleApplyOps([{ type: "note.merge", note_ids: selectedArray }]);
    setSelectedNoteIds(new Set());
  }

  function handleTransposeOctave(direction: "up" | "down") {
    handleApplyOps([{ type: "part.transpose_octave", part_id: part.id, direction }]);
  }

  return (
    <section className="space-y-3 rounded-lg border border-gray-200 p-4">
      <h3 className="text-lg font-semibold text-gray-700">ピアノロール({part.name})</h3>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={triggerUndo}
          disabled={!canUndo || isMutating}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
        >
          元に戻す
        </button>
        <button
          type="button"
          onClick={triggerRedo}
          disabled={!canRedo || isMutating}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
        >
          やり直す
        </button>
        <button
          type="button"
          onClick={handleDelete}
          disabled={!canDelete}
          className={`${BUTTON_CLASS} bg-red-600 hover:bg-red-700`}
        >
          削除
        </button>
        <button
          type="button"
          onClick={handleSplit}
          disabled={!canSplit}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
        >
          分割(中点)
        </button>
        <button
          type="button"
          onClick={handleMerge}
          disabled={!canMerge}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
        >
          結合
        </button>
        <button
          type="button"
          onClick={() => handleTransposeOctave("up")}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
        >
          パート全体を+1オクターブ
        </button>
        <button
          type="button"
          onClick={() => handleTransposeOctave("down")}
          className={`${BUTTON_CLASS} bg-gray-600 hover:bg-gray-700`}
        >
          パート全体を-1オクターブ
        </button>
        <span className="text-xs text-gray-500">
          {selectedArray.length > 0
            ? `${selectedArray.length}件選択中`
            : "クリックで選択(Shiftで複数選択)・ドラッグで移動/範囲選択・右端ドラッグでリサイズ・ダブルクリックで追加・Ctrl+Z/Ctrl+Yで元に戻す/やり直す"}
        </span>
      </div>
      {/* #35: レイヤ表示切替(設計書§12.4)。 */}
      <div className="flex flex-wrap items-center gap-4 text-sm text-gray-600">
        <span className="text-xs font-medium text-gray-500">表示レイヤ:</span>
        {(Object.keys(LAYER_LABEL) as LayerCategory[]).map((category) => (
          <label key={category} className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={visibleLayers.has(category)}
              onChange={() => toggleLayer(category)}
            />
            {LAYER_LABEL[category]}
          </label>
        ))}
      </div>
      {applyOps.isError && (
        <p className="text-sm text-red-600">{(applyOps.error as Error).message}</p>
      )}
      <PianoRoll
        notes={visibleNotes}
        partId={part.id}
        timeSignatures={score.time_signatures}
        divisions={score.divisions}
        selectedNoteIds={selectedNoteIds}
        onSelectionChange={setSelectedNoteIds}
        onApplyOps={handleApplyOps}
      />
      <ScorePreview projectId={projectId} score={score} selectedNoteIds={selectedNoteIds} />
      <Inspector note={inspectedNote} onApplyOps={handleApplyOps} />
    </section>
  );
}
