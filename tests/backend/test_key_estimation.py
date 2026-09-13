"""#38: `pipeline/refine/key_estimation.py`のテスト。

`baseline.py`(#26 L0)の`estimate_key_fifths`との整合性(委譲先の統合による
食い違い防止)は`test_baseline_refine.py`側の既存テストが引き続き通ることで
間接的に確認する。ここでは`estimate_key`固有の`label`/`confidence`を検証する。
"""

from __future__ import annotations

from app.pipeline.refine.key_estimation import estimate_key


def test_no_notes_returns_default_c_major_with_zero_confidence() -> None:
    result = estimate_key([])
    assert result.fifths == 0
    assert result.label == "C major"
    assert result.confidence == 0.0


def test_tonic_and_dominant_weighted_pitches_are_estimated_as_c_major() -> None:
    """単純な音階の列挙だけでは平行調(Am)と区別できないため、主音・属音を

    強調したピッチクラス分布を使う(Krumhansl-Schmuckler調推定の性質、
    実際に対話実行して確認済み)。
    """
    weighted_c_major = [0, 0, 0, 4, 4, 7, 7, 2, 9, 11]
    result = estimate_key(weighted_c_major)
    assert result.fifths == 0
    assert result.label == "C major"
    assert 0.0 <= result.confidence <= 1.0


def test_flat_key_uses_b_instead_of_hyphen() -> None:
    """music21は変ロ短調等を"B-"のようにハイフン表記で返すため、"Bb"へ変換する。"""
    result = estimate_key([10, 0, 1, 3, 5, 6, 8, 10])
    assert result.fifths == -5
    assert result.label == "Bb minor"
    assert "-" not in result.label
