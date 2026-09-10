import { useState } from "react";
import { useApplyScoreOps, useScore } from "../hooks/useScore";
import type { PianoRollNote } from "../lib/pianoRoll";
import { PianoRoll } from "./PianoRoll";

interface PianoRollEditorProps {
  projectId: string;
}

const BUTTON_CLASS =
  "rounded-md px-3 py-1.5 text-sm font-medium text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50";

/**
 * #30/#31: PianoRoll描画+編集操作のコンテナ。ツールバー(削除/分割/結合/
 * ±1オクターブ一括)を結線する。Score IR(採譜=transcribe未実行ならnull)が
 * 無ければ何も表示しない(呼び出し元`ProjectWorkspace`は常にマウントしてよい)。
 *
 * ピアノロール編集UI自体はM2で意図的に見送られていた(ユーザー決定済み、M3の
 * スコープ)。M3 PR1では表示・編集はできるがUndo/Redo(#32)・楽譜プレビュー
 * (#33)・再生同期(#34)・視覚エンコーディング/Inspector(#35)は未実装で、
 * それぞれ後続PRで対応する。
 */
export function PianoRollEditor({ projectId }: PianoRollEditorProps) {
  const { data: score, isLoading, error } = useScore(projectId);
  const applyOps = useApplyScoreOps(projectId);
  const [selectedNoteIds, setSelectedNoteIds] = useState<Set<number>>(new Set());

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
    }));

  const selectedArray = [...selectedNoteIds];
  const canDelete = selectedArray.length >= 1;
  const canSplit = selectedArray.length === 1;
  const canMerge = selectedArray.length >= 2;

  function handleDelete() {
    if (selectedArray.length === 0) return;
    applyOps.mutate([{ type: "note.delete", note_ids: selectedArray }]);
    setSelectedNoteIds(new Set());
  }

  function handleSplit() {
    if (selectedArray.length !== 1) return;
    const note = editableNotes.find((n) => n.id === selectedArray[0]);
    if (!note) return;
    const atTick = note.onset_tick + Math.floor(note.duration_tick / 2);
    if (atTick <= note.onset_tick || atTick >= note.onset_tick + note.duration_tick) return;
    applyOps.mutate([{ type: "note.split", note_id: note.id, at_tick: atTick }]);
  }

  function handleMerge() {
    if (selectedArray.length < 2) return;
    applyOps.mutate([{ type: "note.merge", note_ids: selectedArray }]);
    setSelectedNoteIds(new Set());
  }

  function handleTransposeOctave(direction: "up" | "down") {
    applyOps.mutate([{ type: "part.transpose_octave", part_id: part.id, direction }]);
  }

  return (
    <section className="space-y-3 rounded-lg border border-gray-200 p-4">
      <h3 className="text-lg font-semibold text-gray-700">ピアノロール({part.name})</h3>
      <div className="flex flex-wrap items-center gap-3">
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
            : "クリックで選択(Shiftで複数選択)・ドラッグで移動/範囲選択・右端ドラッグでリサイズ・ダブルクリックで追加"}
        </span>
      </div>
      {applyOps.isError && (
        <p className="text-sm text-red-600">{(applyOps.error as Error).message}</p>
      )}
      <PianoRoll
        notes={editableNotes}
        partId={part.id}
        timeSignatures={score.time_signatures}
        divisions={score.divisions}
        selectedNoteIds={selectedNoteIds}
        onSelectionChange={setSelectedNoteIds}
        onApplyOps={(ops) => applyOps.mutate(ops)}
      />
    </section>
  );
}
