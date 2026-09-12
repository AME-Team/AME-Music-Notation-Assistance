import { type FormEvent, useState } from "react";
import type { NoteOp, ScoreNote } from "../api/client";
import { PROVENANCE_LABEL, provenanceStyle } from "../lib/pianoRoll";

interface InspectorProps {
  /** 単一選択時のみノートを渡す(0件/複数選択時は`null`)。 */
  note: ScoreNote | null;
  onApplyOps: (ops: NoteOp[]) => void;
}

const STATUS_LABEL: Record<string, string> = {
  active: "アクティブ",
  deleted: "削除済み(ピアノロール上でクリックすると復活します)",
  muted: "ミュート",
};

const INPUT_CLASS =
  "w-20 rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500";
const FIELD_LABEL_CLASS = "flex flex-col gap-1 text-sm text-gray-600";
const BUTTON_CLASS =
  "rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50";

/**
 * #35: 選択ノートのプロパティ編集、AIの判断理由と出自runの表示(設計書§12.3)。
 *
 * 単一選択時のみ表示する(0件/複数選択時はヒント文言のみ)。位置(onset_tick)/
 * 音高(midi)/長さ(duration_tick)はピアノロール上のドラッグでも編集できるが、
 * ベロシティ・ボイス・段(staff)はドラッグ操作が無いためInspectorが唯一の
 * 編集手段になる。まとめて`note.update`(#31)で送信する。
 *
 * 出自(`provenance`)・状態(`status`)・信頼度・AIの判断理由・出自run ID・
 * スペリングは読み取り専用(AIが自動で設定するメタデータであり、直接の
 * ユーザー入力対象ではないため)。
 */
export function Inspector({ note, onApplyOps }: InspectorProps) {
  return (
    <section className="space-y-3 rounded-lg border border-gray-200 p-4">
      <h3 className="text-lg font-semibold text-gray-700">Inspector</h3>
      {note ? (
        // 選択ノートが変わるたびにフォームのローカルstateをリセットする必要が
        // あるため、`key={note.id}`で丸ごと再マウントする(`ProjectWorkspace`の
        // `key={projectId}`と同じ「選択が変わったら再マウントしてstateを
        // リセットする」既存パターン)。
        <InspectorForm key={note.id} note={note} onApplyOps={onApplyOps} />
      ) : (
        <p className="text-sm text-gray-500">
          ノートを1件選択するとプロパティを編集できます(複数選択時は非表示)。
        </p>
      )}
    </section>
  );
}

function InspectorForm({
  note,
  onApplyOps,
}: {
  note: ScoreNote;
  onApplyOps: (ops: NoteOp[]) => void;
}) {
  const [midi, setMidi] = useState(String(note.midi));
  const [onsetTick, setOnsetTick] = useState(String(note.onset_tick ?? 0));
  const [durationTick, setDurationTick] = useState(String(note.duration_tick ?? 0));
  const [velocity, setVelocity] = useState(String(note.velocity));
  const [voice, setVoice] = useState(String(note.voice));
  const [staff, setStaff] = useState(String(note.staff));

  // 削除済み/ミュートのノートはプロパティ編集の対象外とする(削除済みは
  // ピアノロール上のクリックで復活させるのが本来の操作導線、設計書§12.4)。
  const isEditable = note.status === "active";
  const style = provenanceStyle(note.provenance);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const midiValue = Number(midi);
    const onsetTickValue = Number(onsetTick);
    const durationTickValue = Number(durationTick);
    const velocityValue = Number(velocity);
    const voiceValue = Number(voice);
    const staffValue = Number(staff);
    if (
      !Number.isFinite(midiValue) ||
      !Number.isFinite(onsetTickValue) ||
      !Number.isFinite(durationTickValue) ||
      !Number.isFinite(velocityValue) ||
      !Number.isFinite(voiceValue) ||
      !Number.isFinite(staffValue)
    ) {
      return;
    }
    onApplyOps([
      {
        type: "note.update",
        note_ids: [note.id],
        midi: midiValue,
        onset_tick: onsetTickValue,
        duration_tick: durationTickValue,
        velocity: velocityValue,
        voice: voiceValue,
        staff: staffValue,
      },
    ]);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 text-sm text-gray-700">
        <span className="flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-3 rounded-sm border border-gray-400"
            style={{ backgroundColor: style.fill }}
          />
          {PROVENANCE_LABEL[note.provenance] ?? note.provenance}
        </span>
        <span className="rounded-md bg-gray-100 px-2 py-0.5 text-xs text-gray-600">
          {STATUS_LABEL[note.status] ?? note.status}
        </span>
        <span className="text-xs text-gray-500">信頼度 {Math.round(note.confidence * 100)}%</span>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600">
        <dt className="font-medium">AIの判断理由</dt>
        <dd>{note.ai_reason ?? "(AI未整音)"}</dd>
        <dt className="font-medium">出自run ID</dt>
        <dd>{note.provenance_run_id ?? "(なし)"}</dd>
        <dt className="font-medium">スペリング</dt>
        <dd>
          {note.spelling
            ? `${note.spelling.step}${note.spelling.alter ? (note.spelling.alter > 0 ? "#" : "b") : ""}${note.spelling.octave}`
            : "(未設定)"}
        </dd>
      </dl>

      {!isEditable && (
        <p className="text-sm text-amber-600">
          {note.status === "deleted"
            ? "削除済みのノートです。ピアノロール上でクリックすると復活します。"
            : "このノートは現在編集できません。"}
        </p>
      )}

      <form className="flex flex-wrap items-end gap-4" onSubmit={handleSubmit}>
        <label className={FIELD_LABEL_CLASS}>
          音高(MIDI)
          <input
            type="number"
            min={0}
            max={127}
            value={midi}
            disabled={!isEditable}
            onChange={(e) => setMidi(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className={FIELD_LABEL_CLASS}>
          開始tick
          <input
            type="number"
            min={0}
            value={onsetTick}
            disabled={!isEditable}
            onChange={(e) => setOnsetTick(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className={FIELD_LABEL_CLASS}>
          長さ(tick)
          <input
            type="number"
            min={1}
            value={durationTick}
            disabled={!isEditable}
            onChange={(e) => setDurationTick(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className={FIELD_LABEL_CLASS}>
          ベロシティ
          <input
            type="number"
            min={0}
            max={127}
            value={velocity}
            disabled={!isEditable}
            onChange={(e) => setVelocity(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className={FIELD_LABEL_CLASS}>
          ボイス
          <input
            type="number"
            min={1}
            max={4}
            value={voice}
            disabled={!isEditable}
            onChange={(e) => setVoice(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className={FIELD_LABEL_CLASS}>
          段(staff)
          <input
            type="number"
            min={1}
            value={staff}
            disabled={!isEditable}
            onChange={(e) => setStaff(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <button type="submit" disabled={!isEditable} className={BUTTON_CLASS}>
          適用
        </button>
      </form>
    </div>
  );
}
