"""#23: 異名同音・音名変換(domain/pitch.py)のプロパティテスト。

V-4(ピッチクラス一致)/V-5(オクターブ整合)の検証ロジック(§9)を、
「自分で生成したMIDI番号に対しては一致する」「意図的にずらすと不一致になる」の
両面から検証する。
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.domain.pitch import is_spelling_consistent_with_midi, spelling_to_midi
from app.domain.score import PitchStep, Spelling

_STEP_VALUES = ("C", "D", "E", "F", "G", "A", "B")


class TestSpellingToMidi:
    def test_c4_is_middle_c(self) -> None:
        """MusicXMLのoctave=4がMIDI60(中央ハ)を含むオクターブであることの基準点。"""
        assert spelling_to_midi(Spelling(step="C", alter=0, octave=4)) == 60

    @given(st.integers(min_value=-2, max_value=2))
    def test_alter_shifts_midi_by_exactly_alter(self, alter: int) -> None:
        base = spelling_to_midi(Spelling(step="C", alter=0, octave=4))
        shifted = spelling_to_midi(Spelling(step="C", alter=alter, octave=4))
        assert shifted == base + alter

    def test_b_sharp_crosses_into_next_octave(self) -> None:
        """B#4 は自然音Bの半音上=MIDI60(C5相当)になる境界ケース(V-5)。"""
        b4 = spelling_to_midi(Spelling(step="B", alter=0, octave=4))
        b_sharp_4 = spelling_to_midi(Spelling(step="B", alter=1, octave=4))
        assert b_sharp_4 == b4 + 1


class TestSpellingConsistency:
    """#23: V-4(ピッチクラス一致)/V-5(オクターブ整合)の検証(§9)。"""

    @given(
        step=st.sampled_from(_STEP_VALUES),
        alter=st.integers(min_value=-2, max_value=2),
        octave=st.integers(min_value=0, max_value=8),
    )
    def test_spelling_derived_midi_is_self_consistent(
        self, step: PitchStep, alter: int, octave: int
    ) -> None:
        """`spelling_to_midi` が生成したMIDI番号に対しては、常に整合すると判定される。"""
        spelling = Spelling(step=step, alter=alter, octave=octave)
        midi = spelling_to_midi(spelling)
        assert is_spelling_consistent_with_midi(spelling, midi)

    @given(
        step=st.sampled_from(_STEP_VALUES),
        alter=st.integers(min_value=-2, max_value=2),
        octave=st.integers(min_value=0, max_value=8),
        offset=st.integers(min_value=1, max_value=11),
    )
    def test_mismatched_midi_is_rejected(
        self, step: PitchStep, alter: int, octave: int, offset: int
    ) -> None:
        """ピッチクラスがずれたMIDI番号に対しては整合しないと判定される。"""
        spelling = Spelling(step=step, alter=alter, octave=octave)
        midi = spelling_to_midi(spelling)
        assert not is_spelling_consistent_with_midi(spelling, midi + offset)
