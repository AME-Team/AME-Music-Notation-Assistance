"""#44: score-mcpアダプタ共通のスキーマ変換ヘルパー(`agent/mcp/_schema.py`)のテスト。"""

from __future__ import annotations

import pytest
from app.agent.mcp._schema import as_bar_range
from app.agent.mcp.tools import ToolError


def test_converts_two_element_list_to_tuple() -> None:
    assert as_bar_range([1, 4]) == (1, 4)


@pytest.mark.parametrize(
    "bars",
    [
        [1],
        [1, 2, 3],
        [],
        ["1", "4"],
        [1.5, 4],
        [True, 4],
    ],
)
def test_rejects_malformed_bar_range(bars: list) -> None:
    with pytest.raises(ToolError, match="bars must be a 2-element list of integers"):
        as_bar_range(bars)
