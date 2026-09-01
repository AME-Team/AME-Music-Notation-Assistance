"""#20: BeatGridEditor 手動補正ロジックのテスト。"""

from __future__ import annotations

import pytest

from app.pipeline.beat import build_beatmap
from app.pipeline.beatmap_edit import (
    apply_bpm_override,
    apply_offset,
    override_time_signature,
    rotate_downbeat,
)

_BASE_BEATS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
_BASE_DOWNBEATS = [0.0, 2.0]


def _base_beatmap() -> dict:
    return build_beatmap(_BASE_BEATS, _BASE_DOWNBEATS).to_dict()


def test_apply_offset_shifts_all_times() -> None:
    result = apply_offset(_base_beatmap(), 0.25)
    assert result["downbeats_sec"] == [0.25, 2.25]
    assert result["beats"][0]["time_sec"] == 0.25


def test_apply_offset_negative_shift() -> None:
    result = apply_offset(_base_beatmap(), -0.5)
    assert result["beats"][0]["time_sec"] == -0.5


def test_apply_offset_zero_is_noop() -> None:
    """回帰(#20-M1レビュー指摘): offset_sec=0はapply_bpm_overrideのbpm<=0と

    同じくno-opパス。呼び出し元(api/media.py)がオブジェクト同一性で真の
    no-opを検出できるよう、入力をそのまま(同一オブジェクトとして)返す。
    """
    beatmap = _base_beatmap()
    assert apply_offset(beatmap, 0) is beatmap


def test_apply_bpm_override_respaces_beats_from_first_beat() -> None:
    # 元は120bpm(0.5秒間隔)。60bpm(1秒間隔)に上書きする。
    result = apply_bpm_override(_base_beatmap(), 60.0)
    times = [b["time_sec"] for b in result["beats"]]
    assert times == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    assert all(t["bpm"] == 60.0 for t in result["tempo_map"])


def test_apply_bpm_override_preserves_downbeat_positions_by_index() -> None:
    result = apply_bpm_override(_base_beatmap(), 60.0)
    # 元のダウンビートは index 0, 4(4拍子の先頭)。間隔が変わっても同じindexを維持する。
    assert result["downbeats_sec"] == [0.0, 4.0]


def test_apply_bpm_override_zero_bpm_is_noop() -> None:
    beatmap = _base_beatmap()
    assert apply_bpm_override(beatmap, 0.0) == beatmap


def test_rotate_downbeat_shifts_to_next_beat() -> None:
    result = rotate_downbeat(_base_beatmap())
    assert result["downbeats_sec"] == [0.5, 2.5]


def test_rotate_downbeat_all_dropped_is_noop() -> None:
    """回帰(#20-M1レビュー指摘の追加ラウンド): 全ダウンビートの回転先が無く

    全滅する場合、それを許すと `_assign_bars` がbarを1のまま増やさなくなり
    (beat_in_barだけが延々と増える)、引き継いだtime_signaturesと矛盾する
    構造になる。ダウンビートを1つも残せない回転はno-opとして入力を無変更で
    返すべき(以前はdownbeats_sec==[]を許容していたが、これ自体が不整合の
    原因だった)。
    """
    beatmap = build_beatmap([0.0, 0.5, 1.0], [1.0]).to_dict()
    result = rotate_downbeat(beatmap)
    assert result is beatmap
    assert result["downbeats_sec"] == [1.0]


def test_override_time_signature_replaces_existing_bar() -> None:
    beatmap = _base_beatmap()
    result = override_time_signature(beatmap, bar=1, numerator=3, denominator=4)
    assert {"bar": 1, "numerator": 3, "denominator": 4} in result["time_signatures"]
    assert len([ts for ts in result["time_signatures"] if ts["bar"] == 1]) == 1


def test_override_time_signature_adds_new_bar_entry_sorted() -> None:
    beatmap = _base_beatmap()  # bar 1, 2 の2小節
    result = override_time_signature(beatmap, bar=2, numerator=3, denominator=4)
    bars = [ts["bar"] for ts in result["time_signatures"]]
    assert bars == sorted(bars)
    assert {"bar": 2, "numerator": 3, "denominator": 4} in result["time_signatures"]


def test_manual_time_signature_survives_subsequent_offset() -> None:
    """回帰テスト: 手動拍子指定は後続のオフセット操作で失われてはならない(FR-04)。"""
    beatmap = override_time_signature(
        _base_beatmap(), bar=1, numerator=3, denominator=4
    )
    result = apply_offset(beatmap, 0.1)
    assert result["time_signatures"] == beatmap["time_signatures"]


def test_manual_time_signature_survives_subsequent_bpm_override() -> None:
    beatmap = override_time_signature(
        _base_beatmap(), bar=1, numerator=3, denominator=4
    )
    result = apply_bpm_override(beatmap, 100.0)
    assert result["time_signatures"] == beatmap["time_signatures"]


def test_manual_time_signature_survives_subsequent_rotate() -> None:
    beatmap = override_time_signature(
        _base_beatmap(), bar=1, numerator=3, denominator=4
    )
    result = rotate_downbeat(beatmap)
    assert result["time_signatures"] == beatmap["time_signatures"]


def test_rotate_drops_time_signature_entries_beyond_new_bar_count() -> None:
    """回帰テスト: rotateで小節数が減った場合、存在しない小節を指す拍子指定を残さない。

    2小節(bar1: 0.0-1.5, bar2: 2.0のみ)の曲で、末尾ダウンビート(2.0)は回転先の
    ビートが無いため脱落し、rotate後は1小節だけが残る。
    """
    beatmap = build_beatmap([0.0, 0.5, 1.0, 1.5, 2.0], [0.0, 2.0]).to_dict()
    assert max(b["bar"] for b in beatmap["beats"]) == 2  # 前提の確認
    beatmap = override_time_signature(beatmap, bar=2, numerator=3, denominator=4)

    result = rotate_downbeat(beatmap)

    max_bar = max((b["bar"] for b in result["beats"]), default=0)
    assert max_bar == 1
    assert all(ts["bar"] <= max_bar for ts in result["time_signatures"])


def test_override_time_signature_rejects_bar_beyond_current_structure() -> None:
    """回帰テスト(#20): 存在しない小節への拍子指定は422相当のValueErrorにする。"""
    beatmap = _base_beatmap()
    max_bar = max(b["bar"] for b in beatmap["beats"])
    with pytest.raises(ValueError, match="out of range"):
        override_time_signature(beatmap, bar=max_bar + 1, numerator=3, denominator=4)


def test_override_time_signature_rejects_bar_zero_or_negative() -> None:
    beatmap = _base_beatmap()
    with pytest.raises(ValueError, match="out of range"):
        override_time_signature(beatmap, bar=0, numerator=4, denominator=4)


def test_rotate_then_override_time_signature_rejects_now_invalid_bar() -> None:
    """回帰テスト(#20): PATCH内でrotate→time_signature_overrideの順に適用すると、
    rotate後に小節数が減っているため、rotate前は有効だったbarが無効になりうる。
    """
    beatmap = build_beatmap([0.0, 0.5, 1.0, 1.5, 2.0], [0.0, 2.0]).to_dict()
    rotated = rotate_downbeat(beatmap)
    assert max(b["bar"] for b in rotated["beats"]) == 1

    with pytest.raises(ValueError, match="out of range"):
        override_time_signature(rotated, bar=2, numerator=3, denominator=4)


def test_override_time_signature_rejects_zero_numerator() -> None:
    """回帰テスト(#20): 0/0 のような無効な拍子を書き込ませない。"""
    beatmap = _base_beatmap()
    with pytest.raises(ValueError, match="numerator"):
        override_time_signature(beatmap, bar=1, numerator=0, denominator=4)


def test_override_time_signature_rejects_zero_denominator() -> None:
    beatmap = _base_beatmap()
    with pytest.raises(ValueError, match="denominator"):
        override_time_signature(beatmap, bar=1, numerator=4, denominator=0)


def test_override_time_signature_rejects_negative_values() -> None:
    beatmap = _base_beatmap()
    with pytest.raises(ValueError, match="numerator"):
        override_time_signature(beatmap, bar=1, numerator=-1, denominator=4)


def test_override_time_signature_rejects_non_power_of_two_denominator() -> None:
    """回帰(#20-M1レビュー指摘): 4/3 のような楽譜として無効な拍子を書き込ませない。"""
    beatmap = _base_beatmap()
    with pytest.raises(ValueError, match="denominator"):
        override_time_signature(beatmap, bar=1, numerator=4, denominator=3)


def test_override_time_signature_identical_value_is_noop() -> None:
    """回帰(#20-M1レビュー指摘の追加ラウンド): 既存と完全に同一の拍子を再指定

    しても実質no-opであり、入力をそのまま(同一オブジェクトとして)返すべき。
    """
    beatmap = _base_beatmap()  # bar 1 は既定で 4/4
    assert (
        override_time_signature(beatmap, bar=1, numerator=4, denominator=4) is beatmap
    )


def test_override_time_signature_accepts_power_of_two_denominators() -> None:
    beatmap = _base_beatmap()
    for denominator in (1, 2, 4, 8, 16, 32):
        result = override_time_signature(
            beatmap, bar=1, numerator=6, denominator=denominator
        )
        assert result["time_signatures"][0]["denominator"] == denominator
