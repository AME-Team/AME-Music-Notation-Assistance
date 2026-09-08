"""#26: L0 決定論的整音のテスト。

異名同音・staff/voice割り当て・ghostフラグ・userノート保護のそれぞれを、
決定論的なロジックとして検証する。調推定(music21)自体はここでは実推論する
(統計アルゴリズムであり神経網ではないため高速、モック不要)。
"""

from __future__ import annotations

from app.pipeline.refine.baseline import (
    GHOST_MAX_DURATION_SEC,
    GHOST_MAX_VELOCITY,
    RefineNoteInput,
    estimate_key_fifths,
    refine_baseline,
)


def _note(
    id: int,
    midi: int,
    onset_tick: int = 0,
    *,
    duration_sec: float = 0.5,
    velocity: int = 90,
    confidence: float = 1.0,
    flags: tuple[str, ...] = (),
    is_user: bool = False,
) -> RefineNoteInput:
    return RefineNoteInput(
        id=id,
        midi=midi,
        onset_tick=onset_tick,
        duration_sec=duration_sec,
        velocity=velocity,
        confidence=confidence,
        flags=flags,
        is_user=is_user,
    )


def _c_major_context(*, start_id: int, start_tick: int) -> list[RefineNoteInput]:
    """調推定を明確にハ長調へ寄せるための文脈ノート群(2オクターブ分のC長調音階)。"""
    scale = [60, 62, 64, 65, 67, 69, 71] * 2
    return [
        _note(start_id + i, midi=midi, onset_tick=start_tick + i * 480)
        for i, midi in enumerate(scale)
    ]


class TestEstimateKeyFifths:
    def test_no_notes_defaults_to_c_major(self) -> None:
        assert estimate_key_fifths([]) == 0

    def test_c_major_scale_is_detected_as_c_major(self) -> None:
        # C,D,E,F,G,A,B(ピッチクラス) x 数回、Cのウェイトが高い分布。
        pitch_classes = [0, 2, 4, 5, 7, 9, 11, 0, 0, 4, 7]
        assert estimate_key_fifths(pitch_classes) == 0

    def test_g_major_scale_is_detected_with_one_sharp(self) -> None:
        # G,A,B,C,D,E,F#(ピッチクラス): 7,9,11,0,2,4,6
        pitch_classes = [7, 9, 11, 0, 2, 4, 6, 7, 7, 11, 2]
        assert estimate_key_fifths(pitch_classes) == 1


class TestRefineBaselineSpelling:
    def test_c_major_notes_get_natural_spellings(self) -> None:
        notes = [_note(1, midi=60, onset_tick=0), _note(2, midi=62, onset_tick=480)]
        result = refine_baseline(notes)

        assert result[1].spelling == ("C", 0, 4)
        assert result[2].spelling == ("D", 0, 4)

    def test_melodic_direction_affects_chromatic_spelling(self) -> None:
        """回帰(#26): 同じピッチクラスでも進行方向次第でスペリングが変わる。

        調推定は分布ベースなので、ハ長調を明確に確立する文脈ノート群を
        先行させてから、判定対象の2音を続ける(#25-M2レビューと同種の理由:
        サンプルが少ないと調推定自体が曖昧になり、テストの意図が伝わらない)。
        """
        context = _c_major_context(start_id=100, start_tick=-14 * 480)
        ascending = [
            *context,
            _note(1, midi=60, onset_tick=0),
            _note(2, midi=61, onset_tick=480),
        ]
        descending = [
            *context,
            _note(1, midi=62, onset_tick=0),
            _note(2, midi=61, onset_tick=480),
        ]

        asc_result = refine_baseline(ascending)
        desc_result = refine_baseline(descending)

        assert asc_result[2].spelling == ("C", 1, 4)  # 上行->シャープ(C#)
        assert desc_result[2].spelling == ("D", -1, 4)  # 下行->フラット(Db)


class TestRefineBaselineStaffAndVoice:
    def test_high_note_goes_to_staff_1_low_note_to_staff_2(self) -> None:
        notes = [_note(1, midi=72, onset_tick=0), _note(2, midi=40, onset_tick=0)]
        result = refine_baseline(notes)

        assert result[1].staff == 1
        assert result[2].staff == 2

    def test_simultaneous_notes_on_the_same_staff_get_distinct_voices_top_down(
        self,
    ) -> None:
        notes = [
            _note(1, midi=72, onset_tick=0),  # 最高音 -> voice1
            _note(2, midi=68, onset_tick=0),  # 中音 -> voice2
            _note(3, midi=64, onset_tick=0),  # 最低音(同staff) -> voice3
        ]
        result = refine_baseline(notes)

        assert result[1].voice == 1
        assert result[2].voice == 2
        assert result[3].voice == 3

    def test_voice_number_is_clamped_to_four_for_dense_chords(self) -> None:
        notes = [_note(i, midi=60 + i, onset_tick=0) for i in range(6)]  # 同staffに6音
        result = refine_baseline(notes)

        assert max(r.voice for r in result.values()) == 4

    def test_notes_at_different_onset_ticks_are_not_grouped(self) -> None:
        notes = [_note(1, midi=72, onset_tick=0), _note(2, midi=68, onset_tick=480)]
        result = refine_baseline(notes)

        # 別タイミングなので両方voice1(同時発音ではない)。
        assert result[1].voice == 1
        assert result[2].voice == 1


class TestRefineBaselineGhostFlags:
    def test_short_and_quiet_and_low_confidence_note_is_flagged(self) -> None:
        note = _note(
            1,
            midi=60,
            duration_sec=GHOST_MAX_DURATION_SEC / 2,
            velocity=GHOST_MAX_VELOCITY - 1,
            confidence=0.1,
        )
        result = refine_baseline([note])
        assert "ghost_candidate" in result[1].flags

    def test_short_but_loud_note_is_not_flagged(self) -> None:
        """3条件すべてを満たさなければフラグは付かない(ANDであることの回帰)。"""
        note = _note(
            1,
            midi=60,
            duration_sec=GHOST_MAX_DURATION_SEC / 2,
            velocity=90,  # 十分大きい
            confidence=0.1,
        )
        result = refine_baseline([note])
        assert "ghost_candidate" not in result[1].flags

    def test_existing_ghost_flag_is_not_duplicated(self) -> None:
        note = _note(1, midi=60, flags=("ghost_candidate",))
        result = refine_baseline([note])
        assert result[1].flags.count("ghost_candidate") == 1


class TestRefineBaselineUserProtection:
    def test_user_note_is_excluded_from_the_result(self) -> None:
        """回帰(#29): provenance="user"相当のノートは一切変更しない。"""
        notes = [_note(1, midi=60, is_user=True), _note(2, midi=64, onset_tick=480)]
        result = refine_baseline(notes)

        assert 1 not in result
        assert 2 in result

    def test_user_note_still_counts_as_prior_context_for_melodic_direction(
        self,
    ) -> None:
        """userノートを挟んでも、直前ノートとしての半音進行判定には使われる。"""
        context = _c_major_context(start_id=100, start_tick=-14 * 480)
        notes = [
            *context,
            _note(1, midi=62, onset_tick=0, is_user=True),
            _note(2, midi=61, onset_tick=480),
        ]
        result = refine_baseline(notes)
        assert result[2].spelling == ("D", -1, 4)  # 下行(62->61)なのでフラット

    def test_user_note_still_occupies_a_voice_slot_in_its_chord(self) -> None:
        """userノートも同時発音のvoice番号を1つ消費する(残りのノートの番号がずれる)。"""
        notes = [
            _note(1, midi=72, onset_tick=0, is_user=True),  # voice1相当を占有
            _note(2, midi=68, onset_tick=0),
        ]
        result = refine_baseline(notes)
        assert 1 not in result
        assert result[2].voice == 2
