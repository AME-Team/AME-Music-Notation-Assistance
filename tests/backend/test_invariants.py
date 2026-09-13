"""#37: 検証層 `domain/invariants.py`(V-1〜V-10, 設計書§9)のテスト。

「採譜の正しさは自動テストしにくいが、IRの不変条件は完全にテストできる」(設計書§10)
という方針に基づき、各ルールを個別に単体テストし、V-4/V-5/V-7/V-8はhypothesisによる
プロパティテストで網羅する。
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.domain.invariants import (
    Decision,
    ValidationNote,
    Violation,
    validate_decisions,
)
from app.domain.pitch import midi_to_spelling
from app.domain.score import Spelling


def _note(
    id: int,
    *,
    editable: bool = True,
    midi: int = 60,
    onset_beat: float = 0.0,
    duration_beat: float = 1.0,
    snap_ids: tuple[str, ...] = ("a", "b"),
    flags: tuple[str, ...] = (),
) -> ValidationNote:
    return ValidationNote(
        id=id,
        editable=editable,
        midi=midi,
        onset_beat=onset_beat,
        duration_beat=duration_beat,
        snap_candidate_ids=list(snap_ids),
        flags=list(flags),
    )


_C4 = Spelling(
    step="C", alter=0, octave=4
)  # デフォルトのnote(midi=60)に対応する正しい表記


def _decision(note_id: int, *, action: str = "keep", **kwargs: object) -> Decision:
    kwargs.setdefault("reason", "test")
    if action == "keep":
        # keep/split_tieはsnap必須(V-3)。V-7/V-8等、snap自体を検証対象としない
        # テストでスプリアスなV-3違反が混入しないよう、既定値を用意する。
        kwargs.setdefault("snap", "a")
        kwargs.setdefault("spelling", _C4)
    return Decision(note_id=note_id, action=action, **kwargs)  # type: ignore[arg-type]


def _rules(violations: list[Violation]) -> list[str]:
    return [v.rule for v in violations]


class TestV1UnknownNoteId:
    def test_unknown_note_id_is_violation(self) -> None:
        notes = [_note(1)]
        decisions = [
            _decision(999, snap="a", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-1"]
        assert violations[0].note_id == 999

    def test_known_note_id_is_not_v1_violation(self) -> None:
        notes = [_note(1)]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-1" not in _rules(violations)


class TestV2ContextNote:
    def test_decision_on_context_note_is_violation(self) -> None:
        notes = [_note(1, editable=False)]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-2"]

    def test_decision_on_editable_note_is_not_v2_violation(self) -> None:
        notes = [_note(1, editable=True)]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-2" not in _rules(violations)


class TestV3SnapCandidate:
    def test_snap_not_in_candidates_is_violation(self) -> None:
        notes = [_note(1, snap_ids=("a", "b"))]
        decisions = [
            _decision(1, snap="z", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-3"]

    def test_snap_in_candidates_is_not_violation(self) -> None:
        notes = [_note(1, snap_ids=("a", "b"))]
        decisions = [
            _decision(1, snap="b", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-3" not in _rules(violations)

    def test_delete_action_does_not_require_snap(self) -> None:
        notes = [_note(1, snap_ids=("a", "b"))]
        decisions = [_decision(1, action="delete")]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        # V-6(delete率)は無関係にここでは検証しない(単一ノートの100%削除は別途
        # 期待通りV-6を発火させる、TestV6DeleteRate参照)。ここで見たいのは
        # deleteアクションがsnap未指定でもV-3を発火させないことだけ。
        assert "V-3" not in _rules(violations)


class TestV4V5Spelling:
    def test_wrong_pitch_class_is_v4_violation(self) -> None:
        notes = [_note(1, midi=60, snap_ids=("a",))]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="D", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-4"]

    def test_correct_pitch_class_wrong_octave_is_v5_violation(self) -> None:
        """B#4はピッチクラスは0でmidi=60と一致するが、実際のMIDI番号は72相当であり

        octaveが不整合となる境界ケース(test_pitch.pyの`is_spelling_consistent_with_midi`
        テストと同じ着眼点)。
        """
        notes = [_note(1, midi=60, snap_ids=("a",))]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="B", alter=1, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-5"]

    def test_correct_spelling_is_not_violation(self) -> None:
        notes = [_note(1, midi=60, snap_ids=("a",))]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    @given(
        st.integers(min_value=0, max_value=127), st.integers(min_value=-7, max_value=7)
    )
    def test_midi_to_spelling_roundtrip_never_violates_v4_v5(
        self, midi: int, fifths: int
    ) -> None:
        """`midi_to_spelling`(#26, 既存L0ロジック)が生成するspellingは常にV-4/V-5を通す。"""
        spelling = midi_to_spelling(midi, fifths=fifths)
        notes = [_note(1, midi=midi, snap_ids=("a",))]
        decisions = [_decision(1, snap="a", spelling=spelling)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-4" not in _rules(violations)
        assert "V-5" not in _rules(violations)


class TestV6DeleteRate:
    def test_delete_rate_within_threshold_is_not_violation(self) -> None:
        # 10notes(時間が重ならないよう1拍ずつずらす), 1 deleted = 10% <= 15%
        notes = [_note(i, onset_beat=float(i)) for i in range(1, 11)]
        decisions = [_decision(1, action="delete")] + [
            _decision(i) for i in range(2, 11)
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-6" not in _rules(violations)

    def test_delete_rate_exceeding_threshold_is_violation(self) -> None:
        # 10notes, 2 deleted = 20% > 15%
        notes = [_note(i, onset_beat=float(i)) for i in range(1, 11)]
        decisions = [_decision(1, action="delete"), _decision(2, action="delete")] + [
            _decision(i) for i in range(3, 11)
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-6"]

    def test_ghost_candidate_allows_higher_delete_rate(self) -> None:
        # 10 ghost notes, 5 deleted = 50% <= 60%(ghost閾値)だが通常閾値15%は超える
        notes = [
            _note(i, onset_beat=float(i), flags=("ghost_candidate",))
            for i in range(1, 11)
        ]
        decisions = [_decision(i, action="delete") for i in range(1, 6)] + [
            _decision(i) for i in range(6, 11)
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    def test_ghost_candidate_exceeding_ghost_threshold_is_violation(self) -> None:
        notes = [
            _note(i, onset_beat=float(i), flags=("ghost_candidate",))
            for i in range(1, 11)
        ]
        decisions = [_decision(i, action="delete") for i in range(1, 8)]  # 70% > 60%
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-6"]

    def test_context_notes_excluded_from_delete_rate(self) -> None:
        """editable=falseのノートはV-1/V-2で別途弾かれるため、V-6の母数にも含めない。"""
        notes = [_note(1, editable=False)] + [
            _note(i, onset_beat=float(i)) for i in range(2, 11)
        ]
        decisions = [_decision(i) for i in range(2, 11)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-6" not in _rules(violations)


class TestV7VoiceStaffRange:
    def test_voice_out_of_range_is_violation(self) -> None:
        notes = [_note(1)]
        decisions = [_decision(1, voice=5)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-7"]

    def test_staff_out_of_range_is_violation(self) -> None:
        notes = [_note(1)]
        decisions = [_decision(1, staff=3)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-7"]

    def test_staff_within_part_staves_is_not_violation(self) -> None:
        notes = [_note(1)]
        decisions = [_decision(1, staff=2)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    @given(st.integers(min_value=1, max_value=4), st.integers(min_value=1, max_value=8))
    def test_voice_and_staff_within_bounds_never_violate_v7(
        self, voice: int, part_staves: int
    ) -> None:
        notes = [_note(1)]
        decisions = [_decision(1, voice=voice, staff=part_staves)]
        violations = validate_decisions(decisions, notes=notes, part_staves=part_staves)
        assert "V-7" not in _rules(violations)

    @given(st.integers(min_value=5, max_value=100))
    def test_voice_above_4_always_violates_v7(self, voice: int) -> None:
        notes = [_note(1)]
        decisions = [_decision(1, voice=voice)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-7" in _rules(violations)


class TestV8VoiceOverlap:
    def test_overlapping_notes_in_same_voice_is_violation(self) -> None:
        notes = [
            _note(1, onset_beat=0.0, duration_beat=2.0),
            _note(2, onset_beat=1.0, duration_beat=1.0),
        ]
        decisions = [_decision(1, voice=1), _decision(2, voice=1)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-8"]

    def test_non_overlapping_notes_in_same_voice_is_not_violation(self) -> None:
        notes = [
            _note(1, onset_beat=0.0, duration_beat=1.0),
            _note(2, onset_beat=1.0, duration_beat=1.0),
        ]
        decisions = [_decision(1, voice=1), _decision(2, voice=1)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    def test_overlapping_notes_in_different_voices_is_not_violation(self) -> None:
        notes = [
            _note(1, onset_beat=0.0, duration_beat=2.0),
            _note(2, onset_beat=1.0, duration_beat=1.0),
        ]
        decisions = [_decision(1, voice=1), _decision(2, voice=2)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    def test_deleted_note_does_not_occupy_time(self) -> None:
        notes = [
            _note(1, onset_beat=0.0, duration_beat=2.0),
            _note(2, onset_beat=1.0, duration_beat=1.0),
        ]
        decisions = [_decision(1, action="delete"), _decision(2, voice=1)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-8" not in _rules(violations)

    @given(
        st.lists(
            st.integers(min_value=0, max_value=400),
            min_size=2,
            max_size=6,
            unique=True,
        )
    )
    def test_sequential_non_overlapping_intervals_never_violate_v8(
        self, raw_starts: list[int]
    ) -> None:
        """0.25刻みの整数から生成することで、隣接区間の間隔が常に0.25以上になることを

        保証する(生の浮動小数点を使うと極端に小さい間隔が生成され、固定長の
        duration設定と衝突してテストが不安定になるため、#37実装時に判明した
        既知の落とし穴)。duration=0.1 < 0.25なので常に重ならない。
        """
        ordered = sorted(x / 4 for x in raw_starts)
        notes = []
        decisions = []
        for i, start in enumerate(ordered):
            notes.append(_note(i + 1, onset_beat=start, duration_beat=0.1))
            decisions.append(_decision(i + 1, voice=1))
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert "V-8" not in _rules(violations)


class TestV9SplitTieRange:
    def test_split_at_beat_within_range_is_not_violation(self) -> None:
        notes = [_note(1, onset_beat=0.0, duration_beat=2.0, snap_ids=("a",))]
        decisions = [
            _decision(
                1,
                action="split_tie",
                snap="a",
                split_at_beat=1.0,
                spelling=Spelling(step="C", alter=0, octave=4),
                voice=1,
            )
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    def test_split_at_beat_outside_range_is_violation(self) -> None:
        notes = [_note(1, onset_beat=0.0, duration_beat=2.0, snap_ids=("a",))]
        decisions = [
            _decision(
                1,
                action="split_tie",
                snap="a",
                split_at_beat=5.0,
                spelling=Spelling(step="C", alter=0, octave=4),
                voice=1,
            )
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-9"]

    def test_split_tie_without_split_at_beat_is_violation(self) -> None:
        """回帰(#37 Gate2レビュー指摘): split_tieなのにsplit_at_beat未指定のdecisionは

        以前はどのルールにも掛からず素通りしていた(V-3のsnap必須チェックとの非対称)。
        """
        notes = [_note(1, onset_beat=0.0, duration_beat=2.0, snap_ids=("a",))]
        decisions = [
            _decision(
                1,
                action="split_tie",
                snap="a",
                spelling=Spelling(step="C", alter=0, octave=4),
                voice=1,
            )
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-9"]


class TestV10ImplicitKeep:
    def test_decisions_need_not_cover_every_editable_note(self) -> None:
        """decisionが無いノートは暗黙keep — 検証対象外であり違反にならない(§9)。"""
        notes = [
            _note(1, onset_beat=0.0),
            _note(2, onset_beat=1.0),
            _note(3, onset_beat=2.0),
        ]
        decisions = [
            _decision(1, snap="a", spelling=Spelling(step="C", alter=0, octave=4))
        ]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert violations == []

    def test_empty_decisions_is_always_valid(self) -> None:
        notes = [_note(i, onset_beat=float(i)) for i in range(1, 5)]
        violations = validate_decisions([], notes=notes, part_staves=2)
        assert violations == []

    def test_implicit_keep_note_overlapping_explicit_decision_is_v8_violation(
        self,
    ) -> None:
        """decisionが無いノート(暗黙keep)も元のvoiceで時間を占有し続けるため、

        明示decisionされた別ノートと同一voice内で重なればV-8違反になる
        (#37 Gate2レビュー指摘: 暗黙keepがV-8の対象外だった偽陰性の回帰テスト)。
        """
        notes = [
            _note(
                1, onset_beat=0.0, duration_beat=2.0
            ),  # decision無し(暗黙keep, voice既定1)
            _note(2, onset_beat=1.0, duration_beat=1.0),
        ]
        decisions = [_decision(2, voice=1)]
        violations = validate_decisions(decisions, notes=notes, part_staves=2)
        assert _rules(violations) == ["V-8"]
