"""#25: Stage 4 決定論的クオンタイズのテスト。

秒→tick変換(拍アンカーによる線形補間)、スナップ候補の生成・採点、
Swing検出のそれぞれを、決定論的なロジックとして厚くテストする(#19の
拍子導出ロジックと同じ理由: ML非依存で決定論的なため、正確性を直接検証できる)。
"""

from __future__ import annotations

import pytest
from app.pipeline.quantize import (
    DEFAULT_TOP_N,
    beat_tick_anchors,
    detect_swing_ratio,
    quantize_note_onsets,
    quantize_pedal_ticks,
    ticks_to_seconds,
)
from app.pipeline.quantize import (
    _denominator_at_bar as denominator_at_bar,
)
from app.pipeline.quantize import (
    _metrical_weight as metrical_weight,
)
from app.pipeline.quantize import (
    _offset_grid_containing_tick as offset_grid_containing_tick,
)
from app.pipeline.quantize import (
    _seconds_to_raw_tick as seconds_to_raw_tick,
)

# 120bpm(四分音符=0.5秒)、4/4で4小節分のビート列。
_BEATS_120BPM_4_4 = [
    {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
    for i in range(16)
]
_TIME_SIGNATURES_4_4 = [{"bar": 1, "numerator": 4, "denominator": 4}]


class TestDenominatorAtBar:
    def test_defaults_to_four_when_no_signatures(self) -> None:
        assert denominator_at_bar([], bar=5) == 4

    def test_forward_fills_from_the_most_recent_change(self) -> None:
        signatures = [
            {"bar": 1, "numerator": 4, "denominator": 4},
            {"bar": 3, "numerator": 6, "denominator": 8},
        ]
        assert denominator_at_bar(signatures, bar=1) == 4
        assert denominator_at_bar(signatures, bar=2) == 4
        assert denominator_at_bar(signatures, bar=3) == 8
        assert denominator_at_bar(signatures, bar=10) == 8


class TestBeatTickAnchors:
    def test_first_bar1_beat_is_tick_zero(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert anchors[0] == (0.0, 0.0)

    def test_quarter_note_ticks_accumulate_by_divisions(self) -> None:
        """4/4の各ビート=四分音符なので、1ビートごとにdivisions(480)ずつ進む。"""
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert [tick for _, tick in anchors[:4]] == [0.0, 480.0, 960.0, 1440.0]

    def test_pickup_beats_bar_zero_are_excluded(self) -> None:
        beats = [
            {"time_sec": -0.5, "beat_in_bar": 0, "bar": 0},
            *_BEATS_120BPM_4_4,
        ]
        anchors = beat_tick_anchors(beats, _TIME_SIGNATURES_4_4, divisions=480)
        assert anchors[0] == (0.0, 0.0)  # bar=0のビートはアンカーに含まれない

    def test_time_signature_change_affects_ticks_per_beat(self) -> None:
        """#25: 6/8では1ビート=8分音符相当なので、四分音符換算で半分の240tick進む。"""
        beats = [
            {"time_sec": 0.0, "beat_in_bar": 1, "bar": 1},
            {"time_sec": 0.5, "beat_in_bar": 2, "bar": 1},
            {"time_sec": 1.0, "beat_in_bar": 3, "bar": 1},
            {"time_sec": 1.5, "beat_in_bar": 4, "bar": 1},
            {"time_sec": 2.0, "beat_in_bar": 1, "bar": 2},
            {"time_sec": 2.25, "beat_in_bar": 2, "bar": 2},
        ]
        signatures = [
            {"bar": 1, "numerator": 4, "denominator": 4},
            {"bar": 2, "numerator": 6, "denominator": 8},
        ]
        anchors = beat_tick_anchors(beats, signatures, divisions=480)
        # bar1(4/4)の4ビート: 0, 480, 960, 1440
        assert [tick for _, tick in anchors[:4]] == [0.0, 480.0, 960.0, 1440.0]
        # bar2(6/8)最初のビートは bar1最後の後 + 480(4/4の最後のビート分)。
        assert anchors[4][1] == 1920.0
        # bar2の2番目のビートは、6/8の1ビート=240tick進んだ位置。
        assert anchors[5][1] == 1920.0 + 240.0


class TestSecondsToRawTick:
    def test_exact_anchor_time_returns_exact_tick(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert seconds_to_raw_tick(0.5, anchors) == 480.0

    def test_midpoint_interpolates_linearly(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert seconds_to_raw_tick(0.25, anchors) == 240.0

    def test_before_first_anchor_extrapolates(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert seconds_to_raw_tick(-0.25, anchors) == -240.0

    def test_after_last_anchor_extrapolates(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        last_time = anchors[-1][0]
        last_tick = anchors[-1][1]
        assert seconds_to_raw_tick(last_time + 0.5, anchors) == last_tick + 480.0

    def test_empty_anchors_returns_zero(self) -> None:
        assert seconds_to_raw_tick(1.0, []) == 0.0


class TestTicksToSeconds:
    """#31: `_seconds_to_raw_tick`の逆変換。ピアノロールでのノート編集(tick空間)から

    `onset_sec`(生データ)を再計算するために使う。
    """

    def test_exact_anchor_tick_returns_exact_seconds(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert ticks_to_seconds(480.0, anchors) == 0.5

    def test_midpoint_interpolates_linearly(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert ticks_to_seconds(240.0, anchors) == 0.25

    def test_before_first_anchor_extrapolates(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        assert ticks_to_seconds(-240.0, anchors) == -0.25

    def test_after_last_anchor_extrapolates(self) -> None:
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        last_time = anchors[-1][0]
        last_tick = anchors[-1][1]
        assert ticks_to_seconds(last_tick + 480.0, anchors) == last_time + 0.5

    def test_empty_anchors_returns_zero(self) -> None:
        assert ticks_to_seconds(100.0, []) == 0.0

    def test_is_the_inverse_of_seconds_to_raw_tick(self) -> None:
        """任意の秒→tick→秒の往復が(浮動小数の誤差範囲内で)元に戻る。"""
        anchors = beat_tick_anchors(
            _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, divisions=480
        )
        for onset_sec in (0.0, 0.1, 0.5, 1.234, 3.9, 7.5):
            tick = seconds_to_raw_tick(onset_sec, anchors)
            assert ticks_to_seconds(tick, anchors) == pytest.approx(onset_sec)


class TestMetricalWeight:
    def test_quarter_note_position_has_highest_weight(self) -> None:
        assert metrical_weight(0.0, divisions=480) == 1.0
        assert metrical_weight(480.0, divisions=480) == 1.0

    def test_eighth_note_only_position_has_lower_weight(self) -> None:
        assert metrical_weight(240.0, divisions=480) == 0.5

    def test_eighth_triplet_only_position(self) -> None:
        assert abs(metrical_weight(160.0, divisions=480) - (1.0 / 3.0)) < 1e-9

    def test_position_off_any_supported_grid_has_zero_weight(self) -> None:
        # 480は1,2,3,4,6,8,12,16,24,32のいずれの分割でも割り切れない中途半端な値。
        assert metrical_weight(7.0, divisions=480) == 0.0


class TestQuantizeNoteOnsets:
    def test_note_on_the_beat_selects_quarter_note_resolution(self) -> None:
        """ちょうど拍上のノートは、最良候補として1/4分解能・誤差0を選ぶべき。"""
        notes = [(1, 0.5, 0.4)]  # onset=0.5s(ちょうど2拍目), duration=0.4s
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        quantized = result[1]
        assert quantized.onset_tick == 480
        best = next(
            c for c in quantized.snap_candidates if c.id == quantized.selected_snap
        )
        assert best.resolution == "1/4"
        assert best.tick == 480

    def test_keeps_up_to_default_top_n_candidates(self) -> None:
        """#25完了条件: 各ノートが複数のスナップ候補を保持し、既定候補が選択されている。"""
        notes = [(1, 0.52, 0.4)]  # わずかに拍からずれた位置
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        quantized = result[1]
        assert len(quantized.snap_candidates) == DEFAULT_TOP_N
        assert quantized.selected_snap in {c.id for c in quantized.snap_candidates}

    def test_note_id_mapping_is_preserved_for_multiple_notes(self) -> None:
        notes = [(10, 0.0, 0.5), (20, 0.5, 0.5), (30, 1.0, 0.5)]
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        assert set(result.keys()) == {10, 20, 30}
        assert result[10].onset_tick == 0
        assert result[20].onset_tick == 480
        assert result[30].onset_tick == 960

    def test_duration_tick_is_at_least_one(self) -> None:
        """縮退した(極端に短い)ノートでも `duration_tick` は0にならない。"""
        notes = [(1, 0.0, 0.0001)]
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)
        assert result[1].duration_tick >= 1

    def test_does_not_mutate_onset_or_duration_seconds(self) -> None:
        """回帰(#25): 量子化はonset_sec/duration_secという生データを一切変更しない

        (テンポマップ修正後の再量子化を可能にするため、§10.1)。呼び出し元の
        タプルそのものは不変(この関数はtickだけを新たに計算して返す)。
        """
        notes = [(1, 0.37, 0.21)]
        original = tuple(notes[0])
        quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)
        assert notes[0] == original

    def test_duration_tick_is_snapped_to_the_selected_onset_resolution(self) -> None:
        """回帰(#25-M2レビュー): durationは選択された分解能の格子に揃える。

        onset=0.5s(拍上、1/4を選択)、offset=1.01s(raw_tick=969.6, 半端な生値)。
        グリッドへ揃えなければ duration_tick は 489(=969.6-480→丸め)という
        1/4格子(480の倍数)に一致しない半端な値になってしまう。
        """
        notes = [(1, 0.5, 0.51)]  # onset=0.5s, offset=1.01s
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        quantized = result[1]
        assert quantized.onset_tick == 480
        assert quantized.duration_tick == 480  # 480の倍数(1/4格子に整合)

    def test_custom_top_n(self) -> None:
        notes = [(1, 0.5, 0.5)]
        result = quantize_note_onsets(
            notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4, top_n=2
        )
        assert len(result[1].snap_candidates) == 2

    def test_duration_for_a_swing_candidate_note_is_not_collapsed(self) -> None:
        """回帰(#25-M2レビュー2巡目): 選択候補が `"1/8-swing"` のとき、

        終端スナップに固定の1/8格子(240刻み)へフォールバックしていた旧実装では、
        swingしたonset(この場合288, 0.6*480)がその格子に乗らないため、短い
        ノートの終端がonsetより手前に丸まり `duration_tick` が1へ不正に潰れて
        いた。onset_tick自身が乗る格子(この場合1/32=60)から終端を求めることで、
        正の妥当な音価(72)が得られることを検証する。
        """
        notes = [(0, 0.3, 52 / 960)]  # raw_onset_tick=288, raw_offset_tick=340
        # detect_swing_ratio が0.6を検出するのに十分なオフビートサンプルを追加する。
        for beat in range(1, 8):
            notes.append((beat, beat * 0.5, 0.05))
            notes.append((100 + beat, beat * 0.5 + 0.3, 0.05))

        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        quantized = result[0]
        best = next(
            c for c in quantized.snap_candidates if c.id == quantized.selected_snap
        )
        assert best.resolution == "1/8-swing"
        assert quantized.onset_tick == 288
        assert quantized.duration_tick == 72  # 1(旧実装のバグ値)ではない


class TestOffsetGridContainingTick:
    def test_returns_the_coarsest_grid_that_the_tick_lands_on(self) -> None:
        """回帰(#25-M2レビュー): swing候補(例: quarter+320)のように分解能ラベルから

        直接格子幅を引けないtickでも、そのtick自身が乗っている最も粗い標準格子
        (この例では1/8Tの160)を逆算できる。
        """
        assert offset_grid_containing_tick(320, 480) == 160

    def test_exact_quarter_returns_quarter_grid(self) -> None:
        assert offset_grid_containing_tick(480, 480) == 480

    def test_no_standard_grid_matches_falls_back_to_finest_grid(self) -> None:
        # 288(=480*0.6)は1,2,3,4,6,8のどの分割の格子にも厳密には乗らない。
        assert offset_grid_containing_tick(288, 480) == 60  # 1/32格子(divisions/8)


class TestSwingDetection:
    def test_straight_eighth_notes_are_not_detected_as_swung(self) -> None:
        # 偶数8分音符(オンビート)と、正確に中間(0.5)のオフビート8分音符。
        raw_ticks = [
            i * 240.0 for i in range(12)
        ]  # 0,240,480,720,... ストレートな8分刻み
        assert detect_swing_ratio(raw_ticks, divisions=480) is None

    def test_swung_eighth_notes_are_detected(self) -> None:
        """#25: オフビートが2/3位置(トリプレットスウィング)に寄っている分布を検出する。"""
        # オンビート(0.0)は各拍先頭、オフビートは0.5ではなく0.667(=320/480)付近。
        raw_ticks = []
        for beat in range(8):
            base = beat * 480.0
            raw_ticks.append(base)  # オンビート
            raw_ticks.append(base + 320.0)  # スウィングしたオフビート(2/3位置)
        ratio = detect_swing_ratio(raw_ticks, divisions=480)
        assert ratio is not None
        assert abs(ratio - (320.0 / 480.0)) < 0.01

    def test_too_few_samples_returns_none(self) -> None:
        raw_ticks = [200.0, 210.0]  # オフビート窓には入るがサンプル数が少なすぎる
        assert detect_swing_ratio(raw_ticks, divisions=480) is None

    def test_quantization_does_not_crash_on_a_swung_song(self) -> None:
        """#25完了条件: スウィングした楽曲で候補生成が破綻しない。

        `_BEATS_120BPM_4_4` は4小節(8秒)分あり、以下のテストデータ(4秒分)を
        カバーするのに十分。ビート列を単純に連結すると `time_sec` が非単調に
        なり `_seconds_to_raw_tick` の二分探索の前提(#25-M2レビュー指摘)が
        壊れるため、水増しはしない。
        """
        notes = []
        note_id = 0
        for beat in range(8):
            base_sec = beat * 0.5
            notes.append((note_id, base_sec, 0.2))
            note_id += 1
            notes.append((note_id, base_sec + 0.5 * (320.0 / 480.0), 0.2))
            note_id += 1

        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        assert len(result) == len(notes)
        for quantized in result.values():
            assert len(quantized.snap_candidates) == DEFAULT_TOP_N
            assert quantized.selected_snap in {c.id for c in quantized.snap_candidates}

    def test_exact_eighth_note_position_selects_eighth_resolution_with_zero_error(
        self,
    ) -> None:
        """回帰(#25-M2レビュー): 誤差0で1/8格子に一致する音は、より重みの大きい

        1/4格子(誤差あり)ではなく1/8を選ぶべき(重みはタイブレークにのみ使う)。
        onset=0.25s -> raw_tick=240(=divisions/2, ちょうど8分ウラ)。
        """
        notes = [(1, 0.25, 0.1)]
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        quantized = result[1]
        best = next(
            c for c in quantized.snap_candidates if c.id == quantized.selected_snap
        )
        assert best.resolution == "1/8"
        assert best.tick == 240

    def test_exact_eighth_triplet_position_selects_triplet_resolution(self) -> None:
        """回帰(#25-M2レビュー): ちょうど3連8分位置(raw_tick=160)は1/8Tを選ぶべき。"""
        notes = [(1, 160.0 / 480.0 * 0.5, 0.1)]
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        quantized = result[1]
        best = next(
            c for c in quantized.snap_candidates if c.id == quantized.selected_snap
        )
        assert best.resolution == "1/8T"
        assert best.tick == 160

    def test_adjacent_offbeat_notes_do_not_collapse_onto_the_same_tick(self) -> None:
        """回帰(#25-M2レビュー): オンビートとオフビートの2音が、量子化後に

        同一tickへ潰れてしまわないこと(誤った採点式では両方が1/4の最寄り拍へ
        引き寄せられ、リズムが消失していた)。
        """
        notes = [(1, 0.0, 0.2), (2, 0.25, 0.2)]  # オンビート(拍0) + ちょうど8分ウラ
        result = quantize_note_onsets(notes, _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4)

        assert result[1].onset_tick != result[2].onset_tick
        assert result[1].onset_tick == 0
        assert result[2].onset_tick == 240


class TestQuantizePedalTicks:
    def test_converts_seconds_to_ticks_via_beat_anchors(self) -> None:
        # 0.5s=1拍=480tick、1.0s=2拍=960tick(120bpm、4/4)。
        result = quantize_pedal_ticks(
            [(0.5, 1.0)], _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4
        )
        assert result == [(480, 960)]

    def test_preserves_order_and_count_for_multiple_pedals(self) -> None:
        result = quantize_pedal_ticks(
            [(0.0, 0.5), (1.0, 1.5)], _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4
        )
        assert result == [(0, 480), (960, 1440)]

    def test_stop_tick_is_at_least_start_tick_plus_one(self) -> None:
        """回帰: 開始と終了が丸めで同一tickになっても、区間が消えない。"""
        result = quantize_pedal_ticks(
            [(0.0, 0.0001)], _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4
        )
        assert result[0][1] > result[0][0]

    def test_start_before_first_beat_is_clamped_to_zero(self) -> None:
        """回帰(#27-M2レビュー): 先頭ビートより前に始まるペダルは、外挿により

        負のtickになりうるため0へクランプする。
        """
        result = quantize_pedal_ticks(
            [(-1.0, 0.5)], _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4
        )
        assert result[0][0] == 0

    def test_stop_after_last_beat_is_clamped_to_score_end(self) -> None:
        """回帰(#27-M2レビュー): 最終ビートより後まで続くペダルは、外挿により

        スコア末尾を超えるtickになりうるため最終アンカーのtickへクランプする。
        """
        last_beat_tick = 480 * (len(_BEATS_120BPM_4_4) - 1)
        result = quantize_pedal_ticks(
            [(0.0, 100.0)], _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4
        )
        assert result[0][1] == last_beat_tick

    def test_degenerate_pedal_entirely_past_the_end_still_has_positive_duration(
        self,
    ) -> None:
        """回帰(#27-M2レビュー2巡目): start/stopが両方ともスコア末尾を超える場合でも、

        クランプ後に長さ0(start_tick == stop_tick)へ潰れない。開始側を
        1tick手前へ寄せてでも非ゼロ長を確保する(MusicXMLの<pedal>で
        startとendが同一tickに並ぶのを避けるため)。
        """
        result = quantize_pedal_ticks(
            [(100.0, 200.0)], _BEATS_120BPM_4_4, _TIME_SIGNATURES_4_4
        )
        start_tick, stop_tick = result[0]
        assert stop_tick > start_tick
