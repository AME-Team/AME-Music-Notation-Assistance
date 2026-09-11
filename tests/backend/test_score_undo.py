"""#32: `services/score_undo.py`(ノート単位スナップショット差分によるUndo/Redo)のテスト。"""

from __future__ import annotations

from app.domain.score import Note, Part, ScoreIR, SourceInfo
from app.domain.score_ops import (
    NoteAddOp,
    NoteDeleteOp,
    NoteMergeOp,
    NoteSplitOp,
    NoteUpdateOp,
    PartTransposeOctaveOp,
)
from app.pipeline.quantize import beat_tick_anchors
from app.services import score_undo
from app.services.score_ops import apply_ops

_BEATS_120BPM_4_4 = [
    {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
    for i in range(16)
]
_TIME_SIGNATURES_4_4 = [{"bar": 1, "numerator": 4, "denominator": 4}]
_ANCHORS = beat_tick_anchors(_BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480)


def _make_score(*, staves: int = 2) -> ScoreIR:
    return ScoreIR(
        project_id="proj_test",
        source=SourceInfo(filename="song.wav", duration_sec=10.0, sample_rate=8000),
        divisions=480,
        parts=[Part(id="piano", name="Piano", midi_program=0, staves=staves)],
    )


def _add_note(score: ScoreIR, **overrides: object) -> Note:
    part = score.find_part("piano")
    assert part is not None
    defaults: dict[str, object] = {
        "onset_sec": 0.0,
        "duration_sec": 0.5,
        "onset_tick": 0,
        "duration_tick": 480,
        "midi": 60,
        "velocity": 90,
        "provenance": "amt",
        "voice": 1,
        "staff": 1,
        "status": "active",
    }
    defaults.update(overrides)
    note = Note(id=score.allocate_note_id(), **defaults)  # type: ignore[arg-type]
    part.notes.append(note)
    return note


def _do_edit(score: ScoreIR, state: score_undo.UndoState, ops: list) -> None:
    """`apply_ops`を実行し、`apply_score_ops`と同じ手順でUndoEntryを記録する。"""
    before = score_undo.snapshot_notes(score)
    apply_ops(score, ops, _ANCHORS)
    after = score_undo.snapshot_notes(score)
    changes = score_undo.diff_snapshots(before, after)
    assert changes  # このテストヘルパーは常に変化のあるopでのみ使う
    entry = score_undo.UndoEntry(
        ops=[op.model_dump(mode="json") for op in ops],
        actor="user",
        ts="2026-01-01T00:00:00Z",
        changes=changes,
    )
    score_undo.record_edit(state, entry)


class TestNoteAddUndoRedo:
    def test_undo_removes_added_note_redo_restores_same_id(self) -> None:
        score = _make_score()
        state = score_undo.UndoState()
        _do_edit(
            score,
            state,
            [NoteAddOp(part_id="piano", onset_tick=0, duration_tick=480, midi=64)],
        )
        part = score.find_part("piano")
        assert part is not None
        added_id = part.notes[0].id

        assert score_undo.apply_undo(score, state) is True
        assert part.notes == []

        assert score_undo.apply_redo(score, state) is True
        assert len(part.notes) == 1
        assert part.notes[0].id == added_id
        assert part.notes[0].midi == 64


class TestNoteUpdateUndoRedo:
    def test_undo_restores_previous_fields_redo_reapplies(self) -> None:
        score = _make_score()
        note = _add_note(score, onset_tick=0, midi=60)
        state = score_undo.UndoState()
        _do_edit(
            score, state, [NoteUpdateOp(note_ids=[note.id], onset_tick=960, midi=64)]
        )
        assert note.onset_tick == 960
        assert note.midi == 64

        assert score_undo.apply_undo(score, state) is True
        assert note.onset_tick == 0
        assert note.midi == 60

        assert score_undo.apply_redo(score, state) is True
        assert note.onset_tick == 960
        assert note.midi == 64


class TestNoteDeleteUndoRedo:
    def test_undo_restores_active_status_redo_re_deletes(self) -> None:
        score = _make_score()
        note = _add_note(score)
        state = score_undo.UndoState()
        _do_edit(score, state, [NoteDeleteOp(note_ids=[note.id])])
        assert note.status == "deleted"

        assert score_undo.apply_undo(score, state) is True
        assert note.status == "active"

        assert score_undo.apply_redo(score, state) is True
        assert note.status == "deleted"


class TestNoteSplitUndoRedo:
    def test_undo_removes_new_note_and_restores_original_span(self) -> None:
        score = _make_score()
        note = _add_note(score, onset_tick=0, duration_tick=480)
        state = score_undo.UndoState()
        _do_edit(score, state, [NoteSplitOp(note_id=note.id, at_tick=240)])
        part = score.find_part("piano")
        assert part is not None
        assert len(part.notes) == 2
        new_note_id = next(n.id for n in part.notes if n.id != note.id)

        assert score_undo.apply_undo(score, state) is True
        assert len(part.notes) == 1
        assert part.notes[0].id == note.id
        assert (part.notes[0].onset_tick, part.notes[0].duration_tick) == (0, 480)

        assert score_undo.apply_redo(score, state) is True
        assert len(part.notes) == 2
        assert {n.id for n in part.notes} == {note.id, new_note_id}


class TestNoteMergeUndoRedo:
    def test_undo_restores_both_notes_redo_remerges(self) -> None:
        score = _make_score()
        a = _add_note(score, onset_tick=0, duration_tick=240, midi=60)
        b = _add_note(score, onset_tick=240, duration_tick=240, midi=60)
        state = score_undo.UndoState()
        _do_edit(score, state, [NoteMergeOp(note_ids=[a.id, b.id])])
        part = score.find_part("piano")
        assert part is not None
        assert a.status == "active"
        assert b.status == "deleted"
        assert a.duration_tick == 480

        assert score_undo.apply_undo(score, state) is True
        assert a.duration_tick == 240
        assert b.status == "active"
        assert b.duration_tick == 240

        assert score_undo.apply_redo(score, state) is True
        assert a.duration_tick == 480
        assert b.status == "deleted"


class TestPartTransposeOctaveUndoRedo:
    def test_undo_restores_original_pitches_redo_reshifts(self) -> None:
        score = _make_score()
        a = _add_note(score, midi=60)
        b = _add_note(score, midi=64)
        state = score_undo.UndoState()
        _do_edit(score, state, [PartTransposeOctaveOp(part_id="piano", direction="up")])
        assert (a.midi, b.midi) == (72, 76)

        assert score_undo.apply_undo(score, state) is True
        assert (a.midi, b.midi) == (60, 64)

        assert score_undo.apply_redo(score, state) is True
        assert (a.midi, b.midi) == (72, 76)


class TestUndoRedoStackBehavior:
    def test_new_edit_clears_redo_stack(self) -> None:
        score = _make_score()
        note = _add_note(score, midi=60)
        state = score_undo.UndoState()
        _do_edit(score, state, [NoteUpdateOp(note_ids=[note.id], midi=64)])
        assert score_undo.apply_undo(score, state) is True
        assert len(state.undone) == 1

        _do_edit(score, state, [NoteUpdateOp(note_ids=[note.id], midi=67)])
        assert state.undone == []
        assert note.midi == 67

    def test_undo_on_empty_stack_is_noop(self) -> None:
        score = _make_score()
        state = score_undo.UndoState()
        assert score_undo.apply_undo(score, state) is False

    def test_redo_on_empty_stack_is_noop(self) -> None:
        score = _make_score()
        state = score_undo.UndoState()
        assert score_undo.apply_redo(score, state) is False

    def test_multiple_undo_redo_round_trip(self) -> None:
        score = _make_score()
        note = _add_note(score, midi=60, onset_tick=0)
        state = score_undo.UndoState()
        _do_edit(score, state, [NoteUpdateOp(note_ids=[note.id], midi=64)])
        _do_edit(score, state, [NoteUpdateOp(note_ids=[note.id], onset_tick=480)])

        assert score_undo.apply_undo(score, state) is True
        assert (note.midi, note.onset_tick) == (64, 0)
        assert score_undo.apply_undo(score, state) is True
        assert (note.midi, note.onset_tick) == (60, 0)
        assert score_undo.apply_undo(score, state) is False

        assert score_undo.apply_redo(score, state) is True
        assert (note.midi, note.onset_tick) == (64, 0)
        assert score_undo.apply_redo(score, state) is True
        assert (note.midi, note.onset_tick) == (64, 480)
        assert score_undo.apply_redo(score, state) is False


class TestSnapshotDiff:
    def test_diff_ignores_unchanged_notes(self) -> None:
        score = _make_score()
        unchanged = _add_note(score, midi=60)
        changed = _add_note(score, midi=64)
        before = score_undo.snapshot_notes(score)
        changed.midi = 67
        after = score_undo.snapshot_notes(score)
        changes = score_undo.diff_snapshots(before, after)
        assert set(changes.keys()) == {str(changed.id)}
        assert str(unchanged.id) not in changes
