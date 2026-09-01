"""#18: Stage 2 ビート推定パイプラインのテスト。

`build_beatmap` はMLに依存しない決定論的な変換なのでモックしたビート列で検証する。
`run_beat_estimation` は実際に `beat-this` を1回実行し、配管(呼び出し→スキーマ変換)
が壊れていないことを確認する(モデルチェックポイントのダウンロードが発生する)。
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from app.pipeline.beat import build_beatmap, run_beat_estimation


def test_build_beatmap_schema() -> None:
    beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    downbeats = [0.0, 2.0]

    result = build_beatmap(beats, downbeats).to_dict()

    assert result["downbeats_sec"] == downbeats
    assert result["beats"][0] == {"time_sec": 0.0, "beat_in_bar": 1, "bar": 1}
    assert result["beats"][4] == {"time_sec": 2.0, "beat_in_bar": 1, "bar": 2}
    assert result["time_signatures"] == [{"bar": 1, "numerator": 4, "denominator": 4}]
    assert len(result["tempo_map"]) == len(beats) - 1
    assert all(t["bpm"] == pytest.approx(120.0) for t in result["tempo_map"])
    assert 0.0 <= result["confidence"] <= 1.0


def test_build_beatmap_confidence_is_low_for_irregular_tempo() -> None:
    steady = build_beatmap([0.0, 0.5, 1.0, 1.5, 2.0], [0.0]).confidence
    irregular = build_beatmap([0.0, 0.5, 0.9, 2.0, 2.05], [0.0]).confidence
    assert steady > irregular


def test_build_beatmap_empty_input() -> None:
    result = build_beatmap([], []).to_dict()
    assert result == {
        "beats": [],
        "downbeats_sec": [],
        "time_signatures": [],
        "tempo_map": [],
        "confidence": 0.0,
    }


def test_build_beatmap_pickup_beat_gets_bar_zero() -> None:
    """回帰(#19): 先頭ダウンビートより前のピックアップ拍は bar=0 として区別する。

    以前は先頭ビートを無条件に bar=1/beat_in_bar=1 扱いしており、ピックアップ拍が
    あると `time_signatures` の集計(count_beats_per_bar)と小節番号がズレていた。
    """
    beats = [-0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]  # 先頭 -0.5 がピックアップ
    downbeats = [0.0, 2.0]

    result = build_beatmap(beats, downbeats).to_dict()

    assert result["beats"][0] == {"time_sec": -0.5, "beat_in_bar": 0, "bar": 0}
    assert result["beats"][1] == {"time_sec": 0.0, "beat_in_bar": 1, "bar": 1}
    # ピックアップ拍が time_signatures の小節数計算に混入していないこと。
    assert result["time_signatures"] == [{"bar": 1, "numerator": 4, "denominator": 4}]
    # tempo_map にも bar=0(ピックアップ)のエントリを出さない(#19レビュー指摘)。
    assert all(t["bar"] >= 1 for t in result["tempo_map"])


def test_build_beatmap_bar_numbering_consistent_under_floating_point_drift() -> None:
    """回帰(#19): beats[].bar と time_signatures[].bar の境界判定基準が一致すること。

    ダウンビート時刻がビート時刻とわずかに(許容誤差未満)ずれていても、
    `_assign_bars` と `count_beats_per_bar` が同じ小節境界に解決される必要がある。
    """
    beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    downbeats = [0.0, 2.0 + 4e-7]  # 許容誤差(1e-6)未満の浮動小数ドリフト

    result = build_beatmap(beats, downbeats).to_dict()

    assert result["beats"][4]["bar"] == 2
    assert result["beats"][4]["beat_in_bar"] == 1
    assert result["time_signatures"] == [{"bar": 1, "numerator": 4, "denominator": 4}]


def test_run_beat_estimation_uses_injected_tracker() -> None:
    """`tracker`を注入できることを確認する(実モデルを待たない配管テスト)。"""

    def fake_tracker(_audio_path: str) -> tuple[list[float], list[float]]:
        return [0.0, 0.5, 1.0, 1.5], [0.0]

    result = run_beat_estimation("dummy.wav", tracker=fake_tracker)
    assert result.downbeats_sec == [0.0]
    assert len(result.beats) == 4


@pytest.mark.slow
def test_run_beat_estimation_with_real_model(tmp_path) -> None:
    """実際に `beat-this` のチェックポイントをダウンロードして推論する(#18の実配管検証)。"""
    sr = 44100
    duration = 4.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    clicks = np.zeros_like(t)
    beat_interval = int(sr * 0.5)
    for i in range(0, len(t), beat_interval):
        end = min(i + 200, len(clicks))
        clicks[i:end] += 0.6 * np.hanning(200)[: end - i]
    audio_path = tmp_path / "clicktrack.wav"
    sf.write(audio_path, clicks.astype(np.float32), sr)

    result = run_beat_estimation(str(audio_path))

    assert len(result.beats) > 0
    assert 0.0 <= result.confidence <= 1.0
