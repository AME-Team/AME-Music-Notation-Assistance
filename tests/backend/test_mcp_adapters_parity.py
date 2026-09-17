"""#44完了条件「同一のtools.pyが2経路の双方から呼び出せる」を直接証明するテスト。

同じseedしたプロジェクトに対し、`score_context`(読み取り専用)と
`score_apply_ops`(唯一の書き込み)を(a)`tools.py`直接、(b)`sdk_adapter`経由、
(c)`stdio_server`経由の3通りで呼び、一貫した結果になることを確認する。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.mcp import sdk_adapter, stdio_server, tools
from app.domain.score import Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import storage

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
    _, via_stdio = await server.call_tool("score_context", {})

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
    _, via_stdio = await server.call_tool("score_apply_ops", {"ops": [op]})

    assert direct["ok"] is via_sdk["ok"] is via_stdio["ok"] is True
    for run_id in ("run_direct", "run_sdk", "run_stdio"):
        staged = storage.read_json(
            storage.score_staging_path(workspace_dir, _PROJECT_ID, run_id)
        )
        staged_part = next(p for p in staged["parts"] if p["id"] == "piano")
        assert [n["midi"] for n in staged_part["notes"]] == [64]
