"""#31: Score IR編集オペレーション(`services/score_ops.py`)のテスト。"""

from __future__ import annotations

import pytest
from app.domain.score import Note, Part, ScoreIR, Spelling, SourceInfo
from app.domain.score_ops import (
    NoteAddOp,
    NoteDeleteOp,
    NoteMergeOp,
    NoteRestoreOp,
    NoteSplitOp,
    NoteUpdateOp,
    PartTransposeOctaveOp,
)
from app.pipeline.quantize import beat_tick_anchors
from app.services.score_ops import ScoreOpError, apply_ops

# 120bpm(四分音符=0.5秒=480tick)、4/4で4小節分のビート列。test_quantize.pyの
# フィクスチャと同じ構成(実際のbeatmapに近い、複数アンカーを持つ変換を検証するため)。
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


class TestNoteAdd:
    def test_adds_note_with_user_provenance_and_synced_timing(self) -> None:
        score = _make_score()
        apply_ops(
            score,
            [NoteAddOp(part_id="piano", onset_tick=480, duration_tick=240, midi=64)],
            _ANCHORS,
        )
        part = score.find_part("piano")
        assert part is not None
        assert len(part.notes) == 1
        note = part.notes[0]
        assert note.provenance == "user"
        assert note.onset_tick == 480
        assert note.duration_tick == 240
        # 120bpmなので480tick=0.5秒、240tick=0.25秒進む。
        assert note.onset_sec == pytest.approx(0.5)
        assert note.duration_sec == pytest.approx(0.25)
        # #31-M3レビュー指摘: 追加ノートにもspellingを設定する
        # (未設定のままだとMusicXMLエクスポートが失敗する)。
        assert note.spelling is not None

    def test_rejects_unknown_part(self) -> None:
        score = _make_score()
        with pytest.raises(ScoreOpError, match="part not found"):
            apply_ops(
                score,
                [
                    NoteAddOp(
                        part_id="nonexistent", onset_tick=0, duration_tick=480, midi=60
                    )
                ],
                _ANCHORS,
            )

    def test_rejects_staff_beyond_part_staves(self) -> None:
        score = _make_score(staves=1)
        with pytest.raises(ScoreOpError, match="exceeds part.staves"):
            apply_ops(
                score,
                [
                    NoteAddOp(
                        part_id="piano",
                        onset_tick=0,
                        duration_tick=480,
                        midi=60,
                        staff=2,
                    )
                ],
                _ANCHORS,
            )


class TestNoteUpdate:
    def test_move_changes_onset_tick_and_resyncs_onset_sec(self) -> None:
        score = _make_score()
        note = _add_note(score)
        apply_ops(score, [NoteUpdateOp(note_ids=[note.id], onset_tick=960)], _ANCHORS)
        assert note.onset_tick == 960
        assert note.onset_sec == pytest.approx(1.0)
        assert note.duration_tick == 480  # 変更していないフィールドは保持
        assert note.provenance == "user"

    def test_resize_changes_duration_tick_and_resyncs_duration_sec(self) -> None:
        score = _make_score()
        note = _add_note(score)
        apply_ops(
            score, [NoteUpdateOp(note_ids=[note.id], duration_tick=960)], _ANCHORS
        )
        assert note.duration_tick == 960
        assert note.duration_sec == pytest.approx(1.0)
        assert note.onset_tick == 0  # 変更していない

    def test_bulk_update_applies_to_multiple_notes(self) -> None:
        score = _make_score()
        a = _add_note(score, onset_tick=0)
        b = _add_note(score, onset_tick=480)
        apply_ops(score, [NoteUpdateOp(note_ids=[a.id, b.id], voice=2)], _ANCHORS)
        assert a.voice == 2
        assert b.voice == 2

    def test_pitch_velocity_and_staff_changes(self) -> None:
        score = _make_score()
        note = _add_note(score)
        apply_ops(
            score,
            [NoteUpdateOp(note_ids=[note.id], midi=72, velocity=100, staff=2)],
            _ANCHORS,
        )
        assert (note.midi, note.velocity, note.staff) == (72, 100, 2)

    def test_pitch_change_recomputes_spelling(self) -> None:
        """#31-M3レビュー指摘: midi変更時にspellingが追随しないとMusicXML

        エクスポートが失敗する(`pipeline/export/score_builder.py`の
        `_spelling_or_raise`)。
        """
        score = _make_score()
        note = _add_note(score, midi=60, spelling=None)
        apply_ops(score, [NoteUpdateOp(note_ids=[note.id], midi=64)], _ANCHORS)
        assert note.spelling is not None
        assert (note.spelling.step, note.spelling.alter, note.spelling.octave) == (
            "E",
            0,
            4,
        )

    def test_octave_only_pitch_change_preserves_step_and_alter(self) -> None:
        """#31-M3レビュー指摘(2巡目): ピッチクラス不変(12の倍数差)のmidi

        変更は`_apply_transpose_octave`と同じくstep/alterを保ちoctaveだけ
        ずらす(fifths=0で一律再計算すると、調号由来の表記が失われ、同じ
        「オクターブ移動」でも操作経路によって挙動が食い違ってしまう)。
        """
        score = _make_score()
        note = _add_note(
            score, midi=66, spelling=Spelling(step="G", alter=-1, octave=4)
        )
        apply_ops(score, [NoteUpdateOp(note_ids=[note.id], midi=78)], _ANCHORS)
        assert note.spelling is not None
        assert (note.spelling.step, note.spelling.alter, note.spelling.octave) == (
            "G",
            -1,
            5,
        )

    def test_rejects_unknown_note_id(self) -> None:
        score = _make_score()
        with pytest.raises(ScoreOpError, match="note not found"):
            apply_ops(score, [NoteUpdateOp(note_ids=[9999], midi=60)], _ANCHORS)

    def test_rejects_staff_beyond_part_staves(self) -> None:
        score = _make_score(staves=1)
        note = _add_note(score)
        with pytest.raises(ScoreOpError, match="exceeds part.staves"):
            apply_ops(score, [NoteUpdateOp(note_ids=[note.id], staff=2)], _ANCHORS)

    def test_rejects_note_without_onset_tick(self) -> None:
        """量子化未実行(#25未到達)のノートはtick空間の編集を受け付けない。"""
        score = _make_score()
        note = _add_note(score, onset_tick=None, duration_tick=None)
        with pytest.raises(ScoreOpError, match="run the quantize stage first"):
            apply_ops(score, [NoteUpdateOp(note_ids=[note.id], midi=60)], _ANCHORS)


class TestNoteDelete:
    def test_soft_deletes_and_marks_user_provenance(self) -> None:
        score = _make_score()
        note = _add_note(score)
        apply_ops(score, [NoteDeleteOp(note_ids=[note.id])], _ANCHORS)
        assert note.status == "deleted"
        assert note.provenance == "user"
        # 物理削除しない(§10.1)。
        part = score.find_part("piano")
        assert part is not None
        assert note in part.notes

    def test_rejects_unknown_note_id(self) -> None:
        score = _make_score()
        with pytest.raises(ScoreOpError, match="note not found"):
            apply_ops(score, [NoteDeleteOp(note_ids=[9999])], _ANCHORS)


class TestNoteRestore:
    def test_restores_a_deleted_note_and_marks_user_provenance(self) -> None:
        score = _make_score()
        note = _add_note(score, status="deleted", provenance="amt")
        apply_ops(score, [NoteRestoreOp(note_ids=[note.id])], _ANCHORS)
        assert note.status == "active"
        assert note.provenance == "user"

    def test_rejects_unknown_note_id(self) -> None:
        score = _make_score()
        with pytest.raises(ScoreOpError, match="note not found"):
            apply_ops(score, [NoteRestoreOp(note_ids=[9999])], _ANCHORS)


class TestNoteSplit:
    def test_splits_into_two_notes_preserving_total_span(self) -> None:
        score = _make_score()
        note = _add_note(
            score, onset_tick=0, duration_tick=480, midi=67, voice=2, staff=2
        )
        apply_ops(score, [NoteSplitOp(note_id=note.id, at_tick=240)], _ANCHORS)

        part = score.find_part("piano")
        assert part is not None
        assert len(part.notes) == 2
        original, new_note = part.notes
        assert (original.onset_tick, original.duration_tick) == (0, 240)
        assert (new_note.onset_tick, new_note.duration_tick) == (240, 240)
        # 分割で生じた両方ともuser所有になり、音高/voice/staffは引き継がれる。
        assert original.provenance == "user"
        assert new_note.provenance == "user"
        assert (new_note.midi, new_note.voice, new_note.staff) == (67, 2, 2)
        assert original.onset_sec == pytest.approx(0.0)
        assert new_note.onset_sec == pytest.approx(0.25)

    def test_rejects_split_point_outside_sounding_range(self) -> None:
        score = _make_score()
        note = _add_note(score, onset_tick=0, duration_tick=480)
        with pytest.raises(ScoreOpError, match="sounding range"):
            apply_ops(score, [NoteSplitOp(note_id=note.id, at_tick=480)], _ANCHORS)
        with pytest.raises(ScoreOpError, match="sounding range"):
            apply_ops(score, [NoteSplitOp(note_id=note.id, at_tick=0)], _ANCHORS)

    def test_new_note_inherits_original_spelling(self) -> None:
        """#31-M3レビュー指摘: 分割はmidiを変えないため、新規ノートには元の

        spellingをそのまま引き継ぐ(fifths=0で再計算すると、元が調号由来の
        表記だった場合に異なる異名同音になりうる)。
        """
        score = _make_score()
        note = _add_note(
            score,
            onset_tick=0,
            duration_tick=480,
            midi=66,
            spelling=Spelling(step="G", alter=-1, octave=4),
        )
        apply_ops(score, [NoteSplitOp(note_id=note.id, at_tick=240)], _ANCHORS)
        part = score.find_part("piano")
        assert part is not None
        _, new_note = part.notes
        assert new_note.spelling is not None
        assert (new_note.spelling.step, new_note.spelling.alter) == ("G", -1)

    def test_new_note_falls_back_to_computed_spelling_when_original_has_none(
        self,
    ) -> None:
        score = _make_score()
        note = _add_note(score, onset_tick=0, duration_tick=480, midi=66, spelling=None)
        apply_ops(score, [NoteSplitOp(note_id=note.id, at_tick=240)], _ANCHORS)
        part = score.find_part("piano")
        assert part is not None
        _, new_note = part.notes
        assert new_note.spelling is not None
        # fifths=0でのmidi=66は F#4(midi_to_spellingの既定の異名同音選択)。
        assert (
            new_note.spelling.step,
            new_note.spelling.alter,
            new_note.spelling.octave,
        ) == (
            "F",
            1,
            4,
        )

    def test_rejects_splitting_a_deleted_note(self) -> None:
        """回帰(#31-M3レビュー指摘): statusを検証しないと削除済みノートが

        分割によって(片方が)復活してしまう(§10.1の論理削除方針に反する)。
        """
        score = _make_score()
        note = _add_note(score, onset_tick=0, duration_tick=480, status="deleted")
        with pytest.raises(ScoreOpError, match="not active"):
            apply_ops(score, [NoteSplitOp(note_id=note.id, at_tick=240)], _ANCHORS)
        part = score.find_part("piano")
        assert part is not None
        assert len(part.notes) == 1  # 新規ノートは生成されない


class TestNoteMerge:
    def test_merges_into_earliest_onset_and_latest_end(self) -> None:
        score = _make_score()
        a = _add_note(score, onset_tick=0, duration_tick=240, midi=60, voice=1, staff=1)
        b = _add_note(
            score, onset_tick=480, duration_tick=240, midi=60, voice=1, staff=1
        )
        apply_ops(score, [NoteMergeOp(note_ids=[a.id, b.id])], _ANCHORS)

        assert a.status == "active"
        assert (a.onset_tick, a.duration_tick) == (0, 720)
        assert b.status == "deleted"
        assert a.provenance == "user"
        assert b.provenance == "user"

    def test_rejects_mismatched_pitch(self) -> None:
        score = _make_score()
        a = _add_note(score, midi=60)
        b = _add_note(score, midi=61)
        with pytest.raises(ScoreOpError, match="same midi/voice/staff"):
            apply_ops(score, [NoteMergeOp(note_ids=[a.id, b.id])], _ANCHORS)

    def test_rejects_notes_from_different_parts(self) -> None:
        score = _make_score()
        part2 = Part(id="other", name="Other", midi_program=0, staves=1)
        score.parts.append(part2)
        a = _add_note(score)
        b_note = Note(
            id=score.allocate_note_id(),
            onset_sec=0.0,
            duration_sec=0.5,
            onset_tick=0,
            duration_tick=480,
            midi=60,
            velocity=90,
            provenance="amt",
        )
        part2.notes.append(b_note)
        with pytest.raises(ScoreOpError, match="different parts"):
            apply_ops(score, [NoteMergeOp(note_ids=[a.id, b_note.id])], _ANCHORS)

    def test_rejects_merging_a_deleted_note(self) -> None:
        """回帰(#31-M3レビュー指摘): statusを検証しないと、削除済みノートが

        primary(生存ノート)候補に選ばれた場合に結合結果全体が無言で削除状態の
        まま残ってしまう(§10.1の論理削除方針に反する)。
        """
        score = _make_score()
        a = _add_note(score, onset_tick=0, duration_tick=240, status="deleted")
        b = _add_note(score, onset_tick=480, duration_tick=240)
        with pytest.raises(ScoreOpError, match="all target notes to be active"):
            apply_ops(score, [NoteMergeOp(note_ids=[a.id, b.id])], _ANCHORS)
        assert b.status == "active"  # bは変更されない(アトミック)

    def test_primary_is_chosen_by_earliest_onset_not_lowest_id(self) -> None:
        """回帰(#31-M3レビュー指摘): id順ではなく、最も早く鳴り始めたノートを

        残す(結合結果のid/provenanceがUndo/Redo(#32)等に影響しうるため)。
        """
        score = _make_score()
        # idが先に割り当てられる方(later_id_but_earlier_onset)を、あえて
        # 後からonset_tickの小さいノートとして追加する。
        first_added_later_onset = _add_note(score, onset_tick=480, duration_tick=240)
        second_added_earlier_onset = _add_note(score, onset_tick=0, duration_tick=240)
        assert first_added_later_onset.id < second_added_earlier_onset.id

        apply_ops(
            score,
            [
                NoteMergeOp(
                    note_ids=[first_added_later_onset.id, second_added_earlier_onset.id]
                )
            ],
            _ANCHORS,
        )
        assert second_added_earlier_onset.status == "active"
        assert first_added_later_onset.status == "deleted"
        assert (
            second_added_earlier_onset.onset_tick,
            second_added_earlier_onset.duration_tick,
        ) == (
            0,
            720,
        )


class TestPartTransposeOctave:
    def test_shifts_active_notes_up_and_down(self) -> None:
        score = _make_score()
        note = _add_note(score, midi=60)
        apply_ops(
            score, [PartTransposeOctaveOp(part_id="piano", direction="up")], _ANCHORS
        )
        assert note.midi == 72
        assert note.provenance == "user"
        apply_ops(
            score, [PartTransposeOctaveOp(part_id="piano", direction="down")], _ANCHORS
        )
        assert note.midi == 60

    def test_does_not_shift_deleted_notes(self) -> None:
        score = _make_score()
        note = _add_note(score, midi=60, status="deleted")
        apply_ops(
            score, [PartTransposeOctaveOp(part_id="piano", direction="up")], _ANCHORS
        )
        assert note.midi == 60

    def test_shift_preserves_step_and_alter_only_octave_changes(self) -> None:
        """#31-M3レビュー指摘: オクターブ移調はピッチクラスを変えないため、既存の

        spellingのstep/alterはそのまま保ち、octaveだけ±1する(fifths=0で丸ごと
        再計算すると元の調号由来の表記と異なる異名同音になりうる)。
        """
        score = _make_score()
        note = _add_note(
            score, midi=66, spelling=Spelling(step="G", alter=-1, octave=4)
        )
        apply_ops(
            score, [PartTransposeOctaveOp(part_id="piano", direction="up")], _ANCHORS
        )
        assert note.spelling is not None
        assert (note.spelling.step, note.spelling.alter, note.spelling.octave) == (
            "G",
            -1,
            5,
        )

    def test_shift_computes_spelling_when_originally_unset(self) -> None:
        score = _make_score()
        note = _add_note(score, midi=60, spelling=None)
        apply_ops(
            score, [PartTransposeOctaveOp(part_id="piano", direction="up")], _ANCHORS
        )
        assert note.spelling is not None

    def test_rejects_shift_that_would_go_out_of_midi_range(self) -> None:
        score = _make_score()
        _add_note(score, midi=120)
        with pytest.raises(ScoreOpError, match="out of range"):
            apply_ops(
                score,
                [PartTransposeOctaveOp(part_id="piano", direction="up")],
                _ANCHORS,
            )

    def test_out_of_range_rejection_is_atomic(self) -> None:
        """1件でも範囲外になるノートがあれば、他のノートも一切変更しない。"""
        score = _make_score()
        safe_note = _add_note(score, midi=60)
        _add_note(score, midi=120)
        with pytest.raises(ScoreOpError):
            apply_ops(
                score,
                [PartTransposeOctaveOp(part_id="piano", direction="up")],
                _ANCHORS,
            )
        assert safe_note.midi == 60


class TestApplyOpsSequencing:
    def test_stops_at_first_failing_op_and_does_not_apply_later_ops(self) -> None:
        score = _make_score()
        note = _add_note(score, midi=60)
        with pytest.raises(ScoreOpError, match="note not found"):
            apply_ops(
                score,
                [
                    NoteUpdateOp(note_ids=[note.id], midi=61),
                    NoteDeleteOp(note_ids=[9999]),
                    NoteUpdateOp(note_ids=[note.id], midi=62),
                ],
                _ANCHORS,
            )
        # 1番目のopは適用されるが、2番目で失敗するため3番目は適用されない。
        assert note.midi == 61
