"""#44完了条件「同一のtools.pyが2経路の双方から呼び出せる」を直接証明するテスト。

同じseedしたプロジェクトに対し、`score_context`(読み取り専用)と
`score_apply_ops`(唯一の書き込み)を(a)`tools.py`直接、(b)`sdk_adapter`経由、
(c)`stdio_server`経由の3通りで呼び、一貫した結果になることを確認する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.agent.mcp import sdk_adapter, stdio_server, tools
from app.domain.score import Note, Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import storage
from mcp.server.fastmcp import FastMCP

_PROJECT_ID = "prj_test"
_BEATMAP = {
    "beats": [
        {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
        for i in range(16)
    ],
    "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
}


def _make_score() -> ScoreIR:
    return ScoreIR(
        project_id=_PROJECT_ID,
        source=SourceInfo(filename="song.wav", duration_sec=10.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[Part(id="piano", name="Piano", midi_program=0, staves=2)],
    )


def _seed_project(workspace_dir: Path, score: ScoreIR) -> None:
    storage.write_json(
        storage.score_current_path(workspace_dir, _PROJECT_ID),
        score.model_dump(mode="json"),
    )
    storage.write_json(storage.beatmap_path(workspace_dir, _PROJECT_ID), _BEATMAP)


def _call_stdio_tool(server: FastMCP, tool_name: str, kwargs: dict[str, object]) -> Any:
    """`server.call_tool()`(FastMCPの簡易メソッド)は戻り値の型注釈次第で

    構造化出力の形が変わる(dictはそのまま、listは`{"result": [...]}`、
    `Any`注釈は構造化出力自体を返さない等、実測して確認した)ため、比較の
    基準として使うには不安定。代わりに登録済みの生のラッパー関数を
    `_tool_manager`経由で直接取得して呼ぶ(`sdk_adapter`側のテストが
    `tool.handler(args)`を直接呼ぶのと同じレベルで比較する)。
    """
    tool = server._tool_manager.get_tool(tool_name)
    assert tool is not None
    return tool.fn(**kwargs)


def _score_with_one_note() -> ScoreIR:
    score = _make_score()
    part = score.find_part("piano")
    assert part is not None
    part.notes.append(
        Note(
            id=score.allocate_note_id(),
            onset_sec=0.0,
            duration_sec=0.5,
            onset_tick=0,
            duration_tick=480,
            midi=60,
            velocity=90,
            provenance="amt",
            voice=1,
            staff=1,
        )
    )
    return score


async def test_score_context_is_consistent_across_both_adapters(
    workspace_dir: Path,
) -> None:
    _seed_project(workspace_dir, _make_score())

    ctx_direct = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_direct"
    )
    direct = tools.score_context(ctx_direct)

    ctx_sdk = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_sdk"
    )
    sdk_tools = {t.name: t for t in sdk_adapter.build_tools(ctx_sdk)}
    sdk_result = await sdk_tools["score_context"].handler({})
    via_sdk = json.loads(sdk_result["content"][0]["text"])

    ctx_stdio = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_stdio"
    )
    server = stdio_server.build_server(ctx_stdio)
    via_stdio = _call_stdio_tool(server, "score_context", {})

    assert direct == via_sdk == via_stdio


async def test_score_apply_ops_is_consistent_across_both_adapters(
    workspace_dir: Path,
) -> None:
    """異なるrun_idで同じopsを3経路とも適用し、それぞれ独立したstagingへ

    同じ結果(ok=True・同じ内容のノート追加)を書き込むことを確認する。
    """
    _seed_project(workspace_dir, _make_score())
    op = {
        "type": "note.add",
        "part_id": "piano",
        "onset_tick": 0,
        "duration_tick": 240,
        "midi": 64,
    }

    ctx_direct = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_direct"
    )
    direct = tools.score_apply_ops(ctx_direct, ops=[op])

    ctx_sdk = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_sdk"
    )
    sdk_tools = {t.name: t for t in sdk_adapter.build_tools(ctx_sdk)}
    sdk_result = await sdk_tools["score_apply_ops"].handler({"ops": [op]})
    via_sdk = json.loads(sdk_result["content"][0]["text"])

    ctx_stdio = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_stdio"
    )
    server = stdio_server.build_server(ctx_stdio)
    via_stdio = _call_stdio_tool(server, "score_apply_ops", {"ops": [op]})

    assert direct["ok"] is via_sdk["ok"] is via_stdio["ok"] is True
    for run_id in ("run_direct", "run_sdk", "run_stdio"):
        staged = storage.read_json(
            storage.score_staging_path(workspace_dir, _PROJECT_ID, run_id)
        )
        staged_part = next(p for p in staged["parts"] if p["id"] == "piano")
        assert [n["midi"] for n in staged_part["notes"]] == [64]


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("score_query", {"part": "piano", "bars": [1, 4], "filter": None}),
        ("score_stats", {"part": "piano", "metric": "pitch_range", "bars": None}),
        ("score_validate", {"scope": None}),
        ("score_render", {"scope": None, "format": "stats"}),
        ("score_note_history", {"note_id": 1}),
    ],
)
async def test_read_only_tool_is_consistent_across_both_adapters(
    workspace_dir: Path, tool_name: str, kwargs: dict[str, object]
) -> None:
    """#44 Gate2レビュー指摘: パリティ検証を`score_context`/`score_apply_ops`

    だけでなく残り5個の読み取り専用ツールにも広げる(手作業で8個ずつ複製した
    ラッパーの引数名・必須/任意のズレは、実際にツールを呼んで比較しない限り
    検知できないため)。`baseline_diff`は常に例外を送出するため別テスト
    (`test_baseline_diff_raises_consistently_across_both_adapters`)で扱う。
    """
    _seed_project(workspace_dir, _score_with_one_note())

    direct_kwargs = dict(kwargs)
    bars = direct_kwargs.get("bars")
    if bars is not None:
        direct_kwargs["bars"] = tuple(bars)  # type: ignore[arg-type]

    ctx_direct = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_direct"
    )
    direct = getattr(tools, tool_name)(ctx_direct, **direct_kwargs)

    ctx_sdk = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_sdk"
    )
    sdk_tools = {t.name: t for t in sdk_adapter.build_tools(ctx_sdk)}
    sdk_result = await sdk_tools[tool_name].handler(kwargs)
    via_sdk = json.loads(sdk_result["content"][0]["text"])

    ctx_stdio = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_stdio"
    )
    server = stdio_server.build_server(ctx_stdio)
    via_stdio = _call_stdio_tool(server, tool_name, kwargs)

    assert direct == via_sdk == via_stdio


async def test_baseline_diff_raises_consistently_across_both_adapters(
    workspace_dir: Path,
) -> None:
    _seed_project(workspace_dir, _make_score())

    ctx_direct = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_direct"
    )
    with pytest.raises(tools.ToolError, match="not implemented"):
        tools.baseline_diff(ctx_direct)

    ctx_sdk = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_sdk"
    )
    sdk_tools = {t.name: t for t in sdk_adapter.build_tools(ctx_sdk)}
    with pytest.raises(tools.ToolError, match="not implemented"):
        await sdk_tools["baseline_diff"].handler({})

    ctx_stdio = tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id="run_stdio"
    )
    server = stdio_server.build_server(ctx_stdio)
    with pytest.raises(tools.ToolError, match="not implemented"):
        _call_stdio_tool(server, "baseline_diff", {})
