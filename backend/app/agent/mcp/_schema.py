"""score-mcpアダプタ共通のスキーマ変換ヘルパー(#44)。

`sdk_adapter.py`/`stdio_server.py`の両方が、JSON経由で届く小節範囲
(`[lo, hi]`のlist)を`tools.py`が要求する`tuple[int, int]`へ変換する必要が
ある。片方だけ修正される事故を防ぐため、この小さな変換ロジックをここへ
集約する(`tools.py`自体はSDK非依存を保つため、ここに置く)。
"""

from __future__ import annotations


def as_bar_range(bars: list[int]) -> tuple[int, int]:
    """JSONの`[lo, hi]`(list)を`tools.py`が要求する2要素tupleへ変換する。

    `tuple(list)`は`tuple[int, ...]`(可変長)型になりmypyの2要素タプル要求を
    満たせないため、明示的に2要素へ展開する。
    """
    lo, hi = bars
    return lo, hi
