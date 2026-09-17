"""score-mcpアダプタ共通のスキーマ変換ヘルパー(#44)。

`sdk_adapter.py`/`stdio_server.py`の両方が、JSON経由で届く小節範囲
(`[lo, hi]`のlist)を`tools.py`が要求する`tuple[int, int]`へ変換する必要が
ある。片方だけ修正される事故を防ぐため、この小さな変換ロジックをここへ
集約する(`tools.py`自体はSDK非依存を保つため、ここに置く)。
"""

from __future__ import annotations

from app.agent.mcp.tools import ToolError


def as_bar_range(bars: list[int]) -> tuple[int, int]:
    """JSONの`[lo, hi]`(list)を`tools.py`が要求する2要素tupleへ変換する。

    `tuple(list)`は`tuple[int, ...]`(可変長)型になりmypyの2要素タプル要求を
    満たせないため、明示的に2要素へ展開する。

    要素数・要素型を検証し、不正な場合は`tools.ToolError`を送出する(#44
    Gate2レビュー指摘: 検証しないと素の`ValueError`/`TypeError`がフレームワーク
    任せの例外変換に委ねられ、SDK/stdio両アダプタで挙動が揃う保証が無い)。
    """
    if len(bars) != 2 or not all(isinstance(b, int) and not isinstance(b, bool) for b in bars):
        raise ToolError(f"bars must be a 2-element list of integers, got {bars!r}")
    lo, hi = bars
    return lo, hi
