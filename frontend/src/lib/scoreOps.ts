import type { NoteOp, ScoreIR, ScoreNote, ScorePart } from "../api/client";

/**
 * サーバへの送信結果を待たずにScore IRへ即座に反映するための、楽観的更新用の
 * 簡易プレビュー変換(#31 §12.5「編集は楽観更新」)。バックエンドの
 * `services/score_ops.py`の`apply_ops`と完全に一致させる必要はない: 成功時は
 * 必ずサーバの権威ある応答(`onset_sec`/`duration_sec`の再計算・実際に採番された
 * ノートIDを含む)でキャッシュを置き換えるため、ここでの目的はドラッグ操作等の
 * 見た目を1フレーム待たせないことだけである。
 *
 * `onset_sec`/`duration_sec`(beatmapを使ったtick→秒変換が必要)は再計算しない
 * (PianoRollはtick空間のフィールドのみで描画するため、この楽観プレビューでは
 * 不要)。同じ理由で`spelling`(異名同音表記)もmidi変更時に追随させない
 * (#31-M3レビュー指摘: バックエンドの`services/score_ops.py`は
 * `_spelling_for_new_midi`でstep/alter/octaveを再計算するが、PianoRollは
 * MIDI番号のみで描画し表記を参照しないため、サーバ応答到着までの短時間
 * `midi`と`spelling`が不整合でも見た目に影響しない。将来notation/OSMD
 * プレビュー(#33)が`spelling`を参照するようになったら、この楽観プレビューも
 * 同じ規則で追随させる必要がある)。バリデーション(voice/staff範囲・
 * split/mergeの整合性等)も行わない: 不正なopはサーバが422/409で拒否し、
 * 呼び出し元(`useApplyScoreOps`)がロールバックする。
 */
export function applyOpsOptimistically(score: ScoreIR, ops: NoteOp[]): ScoreIR {
  const next = structuredClone(score);
  // 呼び出しごとにローカルなカウンタにする(#31-M3レビュー指摘): モジュール
  // スコープの可変変数だとアプリ全体で状態が共有され、再マウントやテスト
  // 実行順に依存した非決定的なIDになってしまう。プレビュー用の仮ID(サーバ
  // 確定前)なので、Score IRの`next_note_id`(1始まりの正の整数)と衝突しない
  // 負の空間を使う。開始値はクローン後のscoreに既に含まれる最小の負ID未満に
  // する(#31-M3レビュー指摘): サーバ応答を待たずに連続適用すると、直前の
  // 呼び出しで追加済みのプレビューノート(例: id=-1)が`score`に残ったまま
  // 新しい呼び出しが再び-1から採番してしまい、重複IDが生じるため。
  const previewId = { next: Math.min(-1, minNoteId(next) - 1) };
  for (const op of ops) {
    applyOne(next, op, previewId);
  }
  return next;
}

function minNoteId(score: ScoreIR): number {
  let min = 0;
  for (const part of score.parts) {
    for (const note of part.notes) {
      if (note.id < min) min = note.id;
    }
  }
  return min;
}

function findPart(score: ScoreIR, partId: string): ScorePart | undefined {
  return score.parts.find((p) => p.id === partId);
}

function findNote(score: ScoreIR, noteId: number): ScoreNote | undefined {
  for (const part of score.parts) {
    const note = part.notes.find((n) => n.id === noteId);
    if (note) return note;
  }
  return undefined;
}

function findOwningPart(score: ScoreIR, note: ScoreNote): ScorePart | undefined {
  return score.parts.find((p) => p.notes.includes(note));
}

function blankNote(overrides: Partial<ScoreNote> & Pick<ScoreNote, "id" | "midi">): ScoreNote {
  return {
    onset_sec: 0,
    duration_sec: 0,
    onset_tick: null,
    duration_tick: null,
    velocity: 90,
    spelling: null,
    voice: 1,
    staff: 1,
    tie: { start: false, stop: false },
    confidence: 1,
    provenance: "user",
    provenance_run_id: null,
    flags: [],
    status: "active",
    snap_candidates: [],
    selected_snap: null,
    ai_reason: null,
    ...overrides,
  };
}

function applyOne(score: ScoreIR, op: NoteOp, previewId: { next: number }): void {
  switch (op.type) {
    case "note.add": {
      const part = findPart(score, op.part_id);
      if (!part) return;
      part.notes.push(
        blankNote({
          id: previewId.next--,
          midi: op.midi,
          onset_tick: op.onset_tick,
          duration_tick: op.duration_tick,
          velocity: op.velocity ?? 90,
          voice: op.voice ?? 1,
          staff: op.staff ?? 1,
        }),
      );
      return;
    }
    case "note.update": {
      for (const noteId of op.note_ids) {
        const note = findNote(score, noteId);
        if (!note) continue;
        // #31-M3レビュー指摘対応: generated型は`number | null`(未指定=変更なし、
        // という意味でnullとundefinedを区別しない、backendのPydantic Optional
        // と同じ意味論)。`!= null`でnull/undefinedの両方を弾く。
        if (op.onset_tick != null) note.onset_tick = op.onset_tick;
        if (op.duration_tick != null) note.duration_tick = op.duration_tick;
        if (op.midi != null) note.midi = op.midi;
        if (op.velocity != null) note.velocity = op.velocity;
        if (op.voice != null) note.voice = op.voice;
        if (op.staff != null) note.staff = op.staff;
        note.provenance = "user";
      }
      return;
    }
    case "note.delete": {
      for (const noteId of op.note_ids) {
        const note = findNote(score, noteId);
        if (!note) continue;
        note.status = "deleted";
        note.provenance = "user";
      }
      return;
    }
    case "note.split": {
      const note = findNote(score, op.note_id);
      if (!note || note.onset_tick === null || note.duration_tick === null) return;
      const endTick = note.onset_tick + note.duration_tick;
      if (!(note.onset_tick < op.at_tick && op.at_tick < endTick)) return;
      const part = findOwningPart(score, note);
      if (!part) return;
      part.notes.push(
        blankNote({
          id: previewId.next--,
          midi: note.midi,
          onset_tick: op.at_tick,
          duration_tick: endTick - op.at_tick,
          velocity: note.velocity,
          voice: note.voice,
          staff: note.staff,
        }),
      );
      note.duration_tick = op.at_tick - note.onset_tick;
      note.provenance = "user";
      return;
    }
    case "note.merge": {
      const notes = op.note_ids
        .map((id) => findNote(score, id))
        .filter(
          (n): n is ScoreNote =>
            n !== undefined && n.onset_tick !== null && n.duration_tick !== null,
        );
      if (notes.length < 2) return;
      const start = Math.min(...notes.map((n) => n.onset_tick as number));
      const end = Math.max(
        ...notes.map((n) => (n.onset_tick as number) + (n.duration_tick as number)),
      );
      const primary = notes.reduce((a, b) =>
        (a.onset_tick as number) <= (b.onset_tick as number) ? a : b,
      );
      primary.onset_tick = start;
      primary.duration_tick = end - start;
      primary.provenance = "user";
      for (const note of notes) {
        if (note !== primary) {
          note.status = "deleted";
          note.provenance = "user";
        }
      }
      return;
    }
    case "part.transpose_octave": {
      const part = findPart(score, op.part_id);
      if (!part) return;
      const delta = op.direction === "up" ? 12 : -12;
      const activeNotes = part.notes.filter((n) => n.status === "active");
      // サーバ(services/score_ops.py)は1件でもmidiが0-127を外れると422で
      // op全体を拒否する(all-or-nothing)。プレビューだけ範囲外のmidiで
      // 一瞬描画されるのを避けるため、同じ判定をここでも行い、はみ出す
      // 場合はプレビューを適用しない(#31-M3レビュー指摘)。
      const wouldOverflow = activeNotes.some((n) => n.midi + delta < 0 || n.midi + delta > 127);
      if (wouldOverflow) return;
      for (const note of activeNotes) {
        note.midi += delta;
        note.provenance = "user";
      }
    }
  }
}
