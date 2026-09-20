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
    MAX_VOICES,
    VOICE_SATURATION_FLAG,
    RefinedNote,
    RefineNoteInput,
    estimate_key_fifths,
    refine_baseline,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# テスト用の量子化格子(divisions=480、4/4)。1拍=480tick、1小節=1920tick。
_TICKS_PER_BEAT = 480
_BAR_TICKS = 4 * _TICKS_PER_BEAT


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


def _validation_notes(
    notes: list[RefineNoteInput], result: dict[int, RefinedNote]
) -> list[ValidationNote]:
    """L0の出力(voice)を検証層V-8へ渡すための`ValidationNote`群を組み立てる。

    1小節(1〜4拍)に収まる前提で、tick位置を小節内beatへ換算する
    (`ValidationNote.onset_beat`は小節内相対値のため小節頭を1.0とする)。
    """
    return [
        ValidationNote(
            id=note.id,
            editable=True,
            midi=note.midi,
            bar=1,
            onset_beat=1.0 + note.onset_tick / _TICKS_PER_BEAT,
            duration_beat=(note.duration_tick or 0) / _TICKS_PER_BEAT,
            voice=result[note.id].voice,
        )
        for note in notes
    ]


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
        validation_notes = _validation_notes(notes, result)

        violations = validate_decisions([], notes=validation_notes, part_staves=1)

        assert violations == []


class TestRefineBaselineVoiceSaturation:
    """4声上限(§7.4/V-7)に達した場合の明示的な挙動(#134)。

    現在の記譜規則では「同一voice・同一時刻の複数ノート」を不正とするため、
    N音が同時に鳴る所はN声を要する。一方で声部数は4に固定されている
    (設計書§7.4・記譜ルール集・検証層V-7)ため、**5音以上の同時発音を含む入力に
    対して重複の無い解は存在しない**(鳩の巣原理)。ここでは「無言で通さない」
    ことを保証する: 上限は守り、残る重複はフラグで明示し、その件数を固定する。
    上限の緩和/ノート削除という根本対応は設計判断のため#134で起票済み。
    """

    def test_four_voice_cap_is_kept_for_five_or_more_simultaneous_notes(self) -> None:
        notes = [
            _note(i, midi=60 + i, onset_tick=0, duration_tick=480) for i in range(1, 7)
        ]
        result = refine_baseline(notes)

        assert max(r.voice for r in result.values()) <= MAX_VOICES

    def test_excess_notes_are_flagged_as_saturated(self) -> None:
        notes = [
            _note(i, midi=60 + i, onset_tick=0, duration_tick=480) for i in range(1, 7)
        ]
        result = refine_baseline(notes)

        flagged = {
            note_id
            for note_id, refined in result.items()
            if VOICE_SATURATION_FLAG in refined.flags
        }

        assert len(flagged) == len(notes) - MAX_VOICES

    def test_remaining_v8_violations_are_never_fewer_than_the_excess(self) -> None:
        """飽和時のV-8違反は「4声上限を超えた分」だけ残る(上限では消せない)。"""
        notes = [
            _note(i, midi=60 + i, onset_tick=0, duration_tick=480) for i in range(1, 7)
        ]
        result = refine_baseline(notes)

        violations = [
            v
            for v in validate_decisions(
                [], notes=_validation_notes(notes, result), part_staves=1
            )
            if v.rule == "V-8"
        ]

        assert len(violations) >= len(notes) - MAX_VOICES

    def test_four_simultaneous_notes_are_exactly_at_the_limit_and_have_no_violation(
        self,
    ) -> None:
        notes = [
            _note(i, midi=60 + i, onset_tick=0, duration_tick=480) for i in range(1, 5)
        ]
        result = refine_baseline(notes)

        assert (
            validate_decisions(
                [], notes=_validation_notes(notes, result), part_staves=1
            )
            == []
        )
        assert all(
            VOICE_SATURATION_FLAG not in refined.flags for refined in result.values()
        )

    def test_saturation_flag_coexists_with_ghost_flag(self) -> None:
        """飽和フラグの付与がghostフラグを消さないこと(両者は独立)。"""
        notes = [
            _note(i, midi=60 + i, onset_tick=0, duration_tick=480) for i in range(1, 5)
        ]
        notes.append(
            _note(
                9,
                midi=60,  # 最も低い音=最後に割り当てられ、飽和する側になる
                onset_tick=0,
                duration_tick=480,
                duration_sec=0.01,
                velocity=GHOST_MAX_VELOCITY - 1,
                confidence=0.1,
            )
        )
        result = refine_baseline(notes)

        ghost_and_saturated = [
            refined
            for refined in result.values()
            if VOICE_SATURATION_FLAG in refined.flags
            and "ghost_candidate" in refined.flags
        ]

        assert len(ghost_and_saturated) == 1
        assert ghost_and_saturated[0].flags.count(VOICE_SATURATION_FLAG) == 1


class TestRefineBaselineV8WithinTheVoiceEnvelope:
    """同時発音が4音以内に収まる入力では、L0の出力が**必ず**V-8を通過すること。

    4声上限は「重なりが最大4音までなら重複なく記譜できる」ことを意味する。
    その範囲でアルゴリズムが破綻しないことをプロパティテストで保証する
    (飽和時は#134のとおり構造上重複が残りうるため、この前提を明示する)。
    """

    @staticmethod
    def _note_stream(data: st.DataObject) -> list[RefineNoteInput]:
        """同時に鳴る音が常に4音以下になるよう制約した和音列を生成する。"""
        notes: list[RefineNoteInput] = []
        next_id = 1
        onset_tick = 0
        for _ in range(data.draw(st.integers(min_value=1, max_value=8))):
            onset_tick += data.draw(st.sampled_from([0, 120, 240, 480, 960]))
            size = data.draw(st.integers(min_value=1, max_value=4))
            duration_tick = data.draw(st.sampled_from([120, 240, 480, 960]))
            sounding = sum(
                1
                for note in notes
                if note.onset_tick + (note.duration_tick or 0) > onset_tick
            )
            # 4声で記譜できない入力(同時5音以上)と、小節(1小節=1920tick)を
            # 越える入力は生成しない。
            if sounding + size > MAX_VOICES or onset_tick + duration_tick >= _BAR_TICKS:
                break
            for index in range(size):
                notes.append(
                    _note(
                        next_id,
                        midi=60 + index * 3,  # 同一staff(>=MIDDLE_C)内の異なる音高
                        onset_tick=onset_tick,
                        duration_tick=duration_tick,
                    )
                )
                next_id += 1
        return notes

    @given(data=st.data())
    @settings(max_examples=50, deadline=None)
    def test_output_never_violates_v8_within_the_envelope(
        self, data: st.DataObject
    ) -> None:
        notes = self._note_stream(data)

        result = refine_baseline(notes)
        violations = validate_decisions(
            [], notes=_validation_notes(notes, result), part_staves=1
        )

        assert violations == []
        assert all(1 <= refined.voice <= MAX_VOICES for refined in result.values())


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
