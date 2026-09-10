import { describe, expect, it } from "vitest";
import type { NoteOp, ScoreIR, ScoreNote, ScorePart } from "../api/client";
import { applyOpsOptimistically } from "./scoreOps";

function note(overrides: Partial<ScoreNote> & Pick<ScoreNote, "id">): ScoreNote {
  return {
    onset_sec: 0,
    duration_sec: 0.5,
    onset_tick: 0,
    duration_tick: 480,
    midi: 60,
    velocity: 90,
    spelling: null,
    voice: 1,
    staff: 1,
    tie: { start: false, stop: false },
    confidence: 1,
    provenance: "amt",
    provenance_run_id: null,
    flags: [],
    status: "active",
    snap_candidates: [],
    selected_snap: null,
    ai_reason: null,
    ...overrides,
  };
}

function part(overrides: Partial<ScorePart> = {}): ScorePart {
  return {
    id: "piano",
    name: "Piano",
    midi_program: 0,
    stem_source: null,
    staves: 2,
    clefs: [],
    notes: [],
    pedals: [],
    ...overrides,
  };
}

function score(parts: ScorePart[]): ScoreIR {
  return {
    schema_version: 3,
    project_id: "proj_test",
    source: { filename: "song.wav", duration_sec: 10, sample_rate: 8000 },
    divisions: 480,
    tempo_map: [],
    time_signatures: [],
    key_signatures: [],
    chords: [],
    parts,
    meta: { stages: {} },
    next_note_id: 1,
  };
}

describe("applyOpsOptimistically", () => {
  it("does not mutate the input score (returns a new object)", () => {
    const s = score([part({ notes: [note({ id: 1 })] })]);
    const op: NoteOp = { type: "note.delete", note_ids: [1] };
    const result = applyOpsOptimistically(s, [op]);
    expect(result).not.toBe(s);
    expect(s.parts[0].notes[0].status).toBe("active"); // 元は変更されない
    expect(result.parts[0].notes[0].status).toBe("deleted");
  });

  it("note.add appends a new active user note with a negative preview id", () => {
    const s = score([part()]);
    const result = applyOpsOptimistically(s, [
      { type: "note.add", part_id: "piano", onset_tick: 480, duration_tick: 240, midi: 64 },
    ]);
    expect(result.parts[0].notes).toHaveLength(1);
    const added = result.parts[0].notes[0];
    expect(added.id).toBeLessThan(0);
    expect(added.provenance).toBe("user");
    expect(added.onset_tick).toBe(480);
    expect(added.duration_tick).toBe(240);
    expect(added.midi).toBe(64);
  });

  it("note.update moves/resizes/repitches only the targeted notes", () => {
    const s = score([part({ notes: [note({ id: 1 }), note({ id: 2, onset_tick: 480 })] })]);
    const result = applyOpsOptimistically(s, [
      { type: "note.update", note_ids: [1], onset_tick: 960, midi: 67 },
    ]);
    const [a, b] = result.parts[0].notes;
    expect(a.onset_tick).toBe(960);
    expect(a.midi).toBe(67);
    expect(a.provenance).toBe("user");
    expect(b.onset_tick).toBe(480); // 対象外のノートは変わらない
  });

  it("note.delete soft-deletes without removing the note", () => {
    const s = score([part({ notes: [note({ id: 1 })] })]);
    const result = applyOpsOptimistically(s, [{ type: "note.delete", note_ids: [1] }]);
    expect(result.parts[0].notes).toHaveLength(1);
    expect(result.parts[0].notes[0].status).toBe("deleted");
  });

  it("note.split creates a second note covering the remainder", () => {
    const s = score([part({ notes: [note({ id: 1, onset_tick: 0, duration_tick: 480 })] })]);
    const result = applyOpsOptimistically(s, [{ type: "note.split", note_id: 1, at_tick: 240 }]);
    expect(result.parts[0].notes).toHaveLength(2);
    const [original, added] = result.parts[0].notes;
    expect(original.duration_tick).toBe(240);
    expect(added.onset_tick).toBe(240);
    expect(added.duration_tick).toBe(240);
  });

  it("note.split is a no-op when at_tick is outside the note's range", () => {
    const s = score([part({ notes: [note({ id: 1, onset_tick: 0, duration_tick: 480 })] })]);
    const result = applyOpsOptimistically(s, [{ type: "note.split", note_id: 1, at_tick: 480 }]);
    expect(result.parts[0].notes).toHaveLength(1);
  });

  it("note.merge extends the earliest note and soft-deletes the rest", () => {
    const s = score([
      part({
        notes: [
          note({ id: 1, onset_tick: 0, duration_tick: 240 }),
          note({ id: 2, onset_tick: 480, duration_tick: 240 }),
        ],
      }),
    ]);
    const result = applyOpsOptimistically(s, [{ type: "note.merge", note_ids: [1, 2] }]);
    const [a, b] = result.parts[0].notes;
    expect(a.status).toBe("active");
    expect(a.onset_tick).toBe(0);
    expect(a.duration_tick).toBe(720);
    expect(b.status).toBe("deleted");
  });

  it("part.transpose_octave shifts only active notes in the given part", () => {
    const s = score([
      part({
        notes: [
          note({ id: 1, midi: 60, status: "active" }),
          note({ id: 2, midi: 60, status: "deleted" }),
        ],
      }),
    ]);
    const result = applyOpsOptimistically(s, [
      { type: "part.transpose_octave", part_id: "piano", direction: "up" },
    ]);
    expect(result.parts[0].notes[0].midi).toBe(72);
    expect(result.parts[0].notes[1].midi).toBe(60); // 削除済みは変更されない
  });

  it("silently ignores ops referencing unknown parts/notes rather than throwing", () => {
    const s = score([part({ notes: [note({ id: 1 })] })]);
    expect(() =>
      applyOpsOptimistically(s, [{ type: "note.delete", note_ids: [9999] }]),
    ).not.toThrow();
  });

  it("part.transpose_octave does not apply when it would push a note out of MIDI range", () => {
    // 回帰(#31-M3レビュー指摘): サーバはall-or-nothingで422拒否するため、
    // プレビューだけ範囲外のmidiで一瞬描画されるべきではない。
    const s = score([part({ notes: [note({ id: 1, midi: 120, status: "active" })] })]);
    const result = applyOpsOptimistically(s, [
      { type: "part.transpose_octave", part_id: "piano", direction: "up" },
    ]);
    expect(result.parts[0].notes[0].midi).toBe(120);
  });

  it("successive optimistic applies do not reuse the same preview note id", () => {
    // 回帰(#31-M3レビュー指摘): サーバ応答を待たずに連続適用すると、直前の
    // 呼び出しで追加済みのプレビューノートと衝突するIDを再度採番してはならない。
    const s = score([part()]);
    const afterFirst = applyOpsOptimistically(s, [
      { type: "note.add", part_id: "piano", onset_tick: 0, duration_tick: 480, midi: 60 },
    ]);
    const afterSecond = applyOpsOptimistically(afterFirst, [
      { type: "note.add", part_id: "piano", onset_tick: 480, duration_tick: 480, midi: 62 },
    ]);
    const ids = afterSecond.parts[0].notes.map((n) => n.id);
    expect(new Set(ids).size).toBe(ids.length); // 重複が無い
  });
});
