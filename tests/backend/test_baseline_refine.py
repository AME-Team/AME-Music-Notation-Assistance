"""#26: L0 決定論的整音のテスト。

異名同音・staff/voice割り当て・ghostフラグ・userノート保護のそれぞれを、
決定論的なロジックとして検証する。調推定(music21)自体はここでは実推論する
(統計アルゴリズムであり神経網ではないため高速、モック不要)。
"""

from __future__ import annotations

from app.domain.invariants import ValidationNote, validate_decisions
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
    duration_tick: int | None = None,
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
        duration_tick=duration_tick,
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


class TestRefineBaselineIntervalVoiceAssignment:
    """#130: 同一onset_tickだけでなく発音区間の重なりでvoiceを分ける。

    以前は`onset_tick`の完全一致でのみグループ化していたため、開始が異なるが
    持続時間が重なる音(アルペジオ、サステインしたまま次の音が鳴るケース等)が
    同一voiceに潰れ、同一voice内の時間重複(V-8違反)になっていた。
    """

    def test_sustained_note_pushes_overlapping_note_to_another_voice(self) -> None:
        notes = [
            _note(1, midi=64, onset_tick=0, duration_tick=960),  # 2拍持続
            _note(2, midi=67, onset_tick=480, duration_tick=480),  # その途中で開始
        ]
        result = refine_baseline(notes)

        assert result[1].voice == 1
        assert result[2].voice == 2  # 同一voice内の時間重複を避ける

    def test_non_overlapping_successive_notes_stay_on_voice_one(self) -> None:
        """回帰(#26): 重なりが無ければ従来どおり単旋律はvoice1のまま。"""
        notes = [
            _note(1, midi=64, onset_tick=0, duration_tick=480),
            _note(2, midi=67, onset_tick=480, duration_tick=480),
            _note(3, midi=65, onset_tick=960, duration_tick=480),
        ]
        result = refine_baseline(notes)

        assert {r.voice for r in result.values()} == {1}

    def test_last_note_ending_exactly_at_next_onset_is_not_overlapping(self) -> None:
        notes = [
            _note(1, midi=64, onset_tick=0, duration_tick=480),
            _note(2, midi=67, onset_tick=480, duration_tick=480),
        ]
        result = refine_baseline(notes)

        assert result[2].voice == 1

    def test_three_overlapping_arpeggio_notes_get_three_distinct_voices(self) -> None:
        notes = [
            _note(1, midi=60, onset_tick=0, duration_tick=1440),
            _note(2, midi=64, onset_tick=480, duration_tick=960),
            _note(3, midi=67, onset_tick=960, duration_tick=480),
        ]
        result = refine_baseline(notes)

        assert [result[i].voice for i in (1, 2, 3)] == [1, 2, 3]

    def test_freed_voice_is_reused_by_a_later_note(self) -> None:
        """voiceは「最後に鳴っていた音が終わった」時点で再利用される(上限を無駄に消費しない)。"""
        notes = [
            _note(1, midi=60, onset_tick=0, duration_tick=960),
            _note(2, midi=64, onset_tick=480, duration_tick=480),  # 重なるのでvoice2
            _note(3, midi=62, onset_tick=960, duration_tick=480),  # voice1が空く
        ]
        result = refine_baseline(notes)

        assert [result[i].voice for i in (1, 2, 3)] == [1, 2, 1]

    def test_assignment_is_independent_of_input_order(self) -> None:
        """決定的であること: 入力順を変えても同じvoiceへ割り当てる。"""
        unordered = [
            _note(3, midi=67, onset_tick=960, duration_tick=480),
            _note(1, midi=60, onset_tick=0, duration_tick=1440),
            _note(2, midi=64, onset_tick=480, duration_tick=960),
        ]
        ordered = list(reversed(unordered))

        first = refine_baseline(unordered)
        second = refine_baseline(ordered)

        assert {i: r.voice for i, r in first.items()} == {
            i: r.voice for i, r in second.items()
        }

    def test_unknown_duration_keeps_same_onset_only_grouping(self) -> None:
        """`duration_tick`未指定(未量子化)は音価不明として従来相当の挙動を保つ。"""
        notes = [
            _note(1, midi=64, onset_tick=0),
            _note(2, midi=67, onset_tick=480),
        ]
        result = refine_baseline(notes)

        assert result[1].voice == 1
        assert result[2].voice == 1

    def test_saturated_voices_stay_within_max_voices(self) -> None:
        """5音以上同時発音では4声に収める(重複は上限に起因する既知の制約)。"""
        notes = [
            _note(i, midi=60 + i, onset_tick=0, duration_tick=480) for i in range(1, 7)
        ]
        result = refine_baseline(notes)

        # 4声を使い切った後の余りは「最も早く終わるvoice」(同点は若い番号)へ回る。
        assert sorted(r.voice for r in result.values()) == [1, 1, 1, 2, 3, 4]

    def test_user_note_occupies_a_voice_for_its_whole_interval(self) -> None:
        """userノートも発音区間を通じてvoiceを1つ占有する(#26から継続する挙動)。"""
        notes = [
            _note(1, midi=72, onset_tick=0, duration_tick=960, is_user=True),
            _note(2, midi=60, onset_tick=480, duration_tick=480),
        ]
        result = refine_baseline(notes, single_staff=True)

        assert 1 not in result
        assert result[2].voice == 2

    def test_staff_voices_are_independent_across_the_grand_staff(self) -> None:
        """ピアノの大譜表: staffごとにvoice番号を1から振る(重なりは互いに影響しない)。"""
        notes = [
            _note(1, midi=72, onset_tick=0, duration_tick=960),  # staff1
            _note(2, midi=48, onset_tick=0, duration_tick=960),  # staff2
        ]
        result = refine_baseline(notes)

        assert (result[1].staff, result[1].voice) == (1, 1)
        assert (result[2].staff, result[2].voice) == (2, 1)

    def test_overlapping_notes_in_a_staff_never_share_a_voice(self) -> None:
        """L0自身の出力がV-8(同一voice内の時間重複)に掛からないこと。

        量子化の格子(divisions)を480tick=1拍として検証層へ渡す。
        """
        notes = [
            _note(1, midi=67, onset_tick=0, duration_tick=1920),  # 4拍持続
            _note(2, midi=64, onset_tick=480, duration_tick=960),
            _note(3, midi=60, onset_tick=960, duration_tick=480),
        ]
        result = refine_baseline(notes)
        validation_notes = [
            ValidationNote(
                id=n.id,
                editable=True,
                midi=n.midi,
                bar=1,
                onset_beat=1.0 + n.onset_tick / 480,
                duration_beat=(n.duration_tick or 0) / 480,
                voice=result[n.id].voice,
            )
            for n in notes
        ]

        violations = validate_decisions([], notes=validation_notes, part_staves=1)

        assert violations == []


class TestRefineBaselineSingleStaff:
    """#56: bass/vocals/guitar/other等、単一譜表しか持たないパート向けの挙動。"""

    def test_single_staff_always_uses_staff_one_regardless_of_pitch(self) -> None:
        notes = [_note(1, midi=72, onset_tick=0), _note(2, midi=40, onset_tick=0)]
        result = refine_baseline(notes, single_staff=True)

        # single_staff=False なら staff 1/2 に分かれる(上のテストで確認済み)が、
        # single_staff=True では低音でもstaff=2へは分けない。
        assert result[1].staff == 1
        assert result[2].staff == 1

    def test_single_staff_chord_gets_distinct_voices_top_down(self) -> None:
        """guitar/other等のポリフォニックな和音が同一voiceに潰れないこと(#56 Gate2レビュー指摘)。"""
        notes = [
            _note(1, midi=64, onset_tick=0),  # E4 最高音 -> voice1
            _note(2, midi=60, onset_tick=0),  # C4 -> voice2
            _note(3, midi=52, onset_tick=0),  # E3 最低音 -> voice3
        ]
        result = refine_baseline(notes, single_staff=True)

        assert result[1].voice == 1
        assert result[2].voice == 2
        assert result[3].voice == 3
        assert {r.staff for r in result.values()} == {1}

    def test_single_staff_monophonic_notes_stay_on_voice_one(self) -> None:
        """bass/vocals(モノフォニック)は別タイミングなので両方voice1のまま。"""
        notes = [_note(1, midi=40, onset_tick=0), _note(2, midi=43, onset_tick=480)]
        result = refine_baseline(notes, single_staff=True)

        assert result[1].voice == 1
        assert result[2].voice == 1


class TestRefineBaselineSharedFifths:
    """#56: 複数パートへ個別に呼ぶ際、事前計算した調号を共有できること。"""

    def test_explicit_fifths_overrides_self_estimation(self) -> None:
        """`fifths`を明示すると、`notes`自身からの調号自己推定をスキップして使われる。"""
        notes = [_note(1, midi=65, onset_tick=0)]  # F4
        result_c_major = refine_baseline(notes, fifths=0)
        result_g_major = refine_baseline(notes, fifths=1)

        assert result_c_major[1].spelling == ("F", 0, 4)  # ハ長調ではFナチュラル
        assert result_g_major[1].spelling == ("E", 1, 4)  # ト長調ではE#


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
