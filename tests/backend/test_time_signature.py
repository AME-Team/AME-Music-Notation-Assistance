"""#19 Q-16: 拍子導出ロジックの網羅テスト。

`beat-this` の実出力に依存せず、合成したビート/ダウンビート配列で決定論的な
ロジックだけを検証する(このモジュールが外部依存ゼロである理由そのもの)。
"""

from __future__ import annotations

from app.pipeline.time_signature import (
    TimeSignature,
    bar_start_ticks,
    count_beats_per_bar,
    derive_time_signatures,
    downbeat_indices,
    find_close_index,
    tick_to_bar_beat,
)


def _beats(
    *, bpm: float, beats_per_bar: int, bars: int
) -> tuple[list[float], list[float]]:
    """一定テンポ・一定拍子のビート/ダウンビート列を合成する。"""
    interval = 60.0 / bpm
    total_beats = beats_per_bar * bars
    beats = [round(i * interval, 6) for i in range(total_beats)]
    downbeats = [beats[i] for i in range(0, total_beats, beats_per_bar)]
    return beats, downbeats


def test_four_four() -> None:
    beats, downbeats = _beats(bpm=120, beats_per_bar=4, bars=8)
    sigs = derive_time_signatures(downbeats, beats)
    assert sigs == [TimeSignature(bar=1, numerator=4, denominator=4)]


def test_three_four() -> None:
    beats, downbeats = _beats(bpm=90, beats_per_bar=3, bars=6)
    sigs = derive_time_signatures(downbeats, beats)
    assert sigs == [TimeSignature(bar=1, numerator=3, denominator=4)]


def test_six_eight_heuristic() -> None:
    """Q-16: 6拍/小節は複合拍子(6/8)とみなす経験則。"""
    beats, downbeats = _beats(bpm=140, beats_per_bar=6, bars=4)
    sigs = derive_time_signatures(downbeats, beats)
    assert sigs == [TimeSignature(bar=1, numerator=6, denominator=8)]


def test_time_signature_change_is_detected() -> None:
    """区間ごとの拍子変化: 最初の2小節が4/4、以降が3/4に変わる曲。"""
    four_beats, four_downbeats = _beats(bpm=120, beats_per_bar=4, bars=2)
    offset = four_beats[-1] + 0.5
    three_beats, three_downbeats = _beats(bpm=120, beats_per_bar=3, bars=3)
    beats = four_beats + [round(b + offset, 6) for b in three_beats]
    downbeats = four_downbeats + [round(d + offset, 6) for d in three_downbeats]

    sigs = derive_time_signatures(downbeats, beats)
    assert sigs == [
        TimeSignature(bar=1, numerator=4, denominator=4),
        TimeSignature(bar=3, numerator=3, denominator=4),
    ]


def test_no_change_produces_single_entry_even_with_many_bars() -> None:
    beats, downbeats = _beats(bpm=100, beats_per_bar=4, bars=50)
    sigs = derive_time_signatures(downbeats, beats)
    assert len(sigs) == 1


def test_empty_input_returns_empty_list() -> None:
    assert derive_time_signatures([], []) == []
    assert count_beats_per_bar([], []) == []


def test_last_partial_bar_counts_remaining_beats() -> None:
    """最後の小節は次のダウンビートが無いため、残り全ビートを1小節として数える。"""
    downbeats = [0.0, 2.0]
    beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]  # 最後の小節は3拍のみ(曲の途中で切れる)
    counts = count_beats_per_bar(downbeats, beats)
    assert counts == [4, 3]


def test_downbeat_with_no_matching_beat_is_ignored() -> None:
    """回帰: ビート列に実在しないダウンビート(不整合な入力)は境界として数えない。

    以前は `round(t, 6)` の窓判定で「一致するビートが無くても1小節分カウントする」
    実装だったが、`beat.py::_assign_bars` とインデックスベースで基準を統一した結果、
    対応するビートが無いダウンビートは(#19レビュー指摘どおり)無視するようになった。
    """
    downbeats = [0.0, 0.1, 5.0]  # 0.1 はどのビートとも一致しない
    beats = [0.0, 5.0]
    counts = count_beats_per_bar(downbeats, beats)
    assert counts == [1, 1]  # 実在する 0.0 と 5.0 の2つの境界だけが有効


def test_downbeat_indices_ignores_unmatched_downbeats() -> None:
    assert downbeat_indices([0.0, 5.0], [0.0, 0.1, 5.0]) == [0, 1]


def test_find_close_index_respects_tolerance() -> None:
    times = [0.0, 0.5, 1.0]
    assert find_close_index(times, 0.5000001) == 1
    assert find_close_index(times, 0.6) is None


class TestBarStartTicksAndTickToBarBeat:
    """#38: `bar_start_ticks`(`score_builder.py`から昇格)と、その逆変換

    `tick_to_bar_beat`(L1チャンク入力の`bar`/`raw_beat`用に新設)のテスト。
    両者が同じ小節境界解釈を共有していることを、`bar_start_ticks`が返す
    各小節開始tickを`tick_to_bar_beat`に通すと必ず`beat=1.0`に戻ることで
    確認する。
    """

    def test_default_4_4_bar_starts(self) -> None:
        starts = bar_start_ticks([], 480, 3)
        assert starts == {1: 0, 2: 1920, 3: 3840}

    def test_time_signature_change_shifts_subsequent_bar_lengths(self) -> None:
        time_signatures = [
            {"bar": 1, "numerator": 4, "denominator": 4},
            {"bar": 3, "numerator": 3, "denominator": 4},
        ]
        starts = bar_start_ticks(time_signatures, 480, 5)
        assert starts == {1: 0, 2: 1920, 3: 3840, 4: 5280, 5: 6720}

    def test_tick_to_bar_beat_at_bar_start_is_beat_one(self) -> None:
        time_signatures = [
            {"bar": 1, "numerator": 4, "denominator": 4},
            {"bar": 3, "numerator": 3, "denominator": 4},
        ]
        starts = bar_start_ticks(time_signatures, 480, 5)
        for bar, tick in starts.items():
            assert tick_to_bar_beat(
                tick, time_signatures=time_signatures, divisions=480
            ) == (
                bar,
                1.0,
            )

    def test_tick_to_bar_beat_mid_bar(self) -> None:
        time_signatures = [{"bar": 1, "numerator": 4, "denominator": 4}]
        # 480 ticks = 1拍(四分音符)。小節2開始(tick 1920)+960 tick = 2拍進んだ位置。
        bar, beat = tick_to_bar_beat(
            1920 + 960, time_signatures=time_signatures, divisions=480
        )
        assert bar == 2
        assert beat == 3.0

    def test_tick_to_bar_beat_roundtrips_with_bar_start_ticks_across_time_signature_change(
        self,
    ) -> None:
        """小節ごとに拍子が変わっても、両関数の小節境界解釈が一致すること

        (#38: 別々に実装すると、エクスポートとL1チャンク分割で小節番号が
        ずれるバグを作り込みうるため、この一致を明示的に固定化する)。
        """
        time_signatures = [
            {"bar": 1, "numerator": 4, "denominator": 4},
            {"bar": 2, "numerator": 6, "denominator": 8},
            {"bar": 3, "numerator": 3, "denominator": 4},
        ]
        starts = bar_start_ticks(time_signatures, 480, 4)
        for bar in range(1, 4):
            computed_bar, computed_beat = tick_to_bar_beat(
                starts[bar], time_signatures=time_signatures, divisions=480
            )
            assert (computed_bar, computed_beat) == (bar, 1.0)
