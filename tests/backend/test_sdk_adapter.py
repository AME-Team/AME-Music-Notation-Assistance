"""#44: score-mcpインプロセスアダプタ(`agent/mcp/sdk_adapter.py`)のテスト。

実MCPプロトコル層(`create_sdk_mcp_server`が構築する`mcp.server.Server`)は
経由せず、`build_tools(ctx)`が返す`SdkMcpTool.handler`を直接呼び、
tools.pyへの委譲ロジックの正しさのみを検証する(#44の完了条件「同一の
tools.pyが2経路の双方から呼び出せる」の実体は`test_mcp_adapters_parity.py`
で確認する)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agent.mcp import sdk_adapter, tools
from app.domain.score import (
    Note,
    Part,
    ScoreIR,
    SourceInfo,
    Spelling,
    TimeSignatureEntry,
)
from app.infra import storage
from mcp.server import Server

_PROJECT_ID = "prj_test"
_RUN_ID = "run_test01"
_BEATMAP = {
    "beats": [
        {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
        for i in range(16)
    ],
    "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
}


def _make_score(*, staves: int = 2) -> ScoreIR:
    return ScoreIR(
        project_id=_PROJECT_ID,
        source=SourceInfo(filename="song.wav", duration_sec=10.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[Part(id="piano", name="Piano", midi_program=0, staves=staves)],
    )


def _add_note(score: ScoreIR, **overrides: object) -> Note:
    part = score.find_part("piano")
    assert part is not None
    defaults: dict[str, object] = {
        "onset_sec": 0.0,
        "duration_sec": 0.5,
        "onset_tick": 0,
        "duration_tick": 480,
        "midi": 60,
        "velocity": 90,
        "provenance": "amt",
        "voice": 1,
        "staff": 1,
        "status": "active",
    }
    defaults.update(overrides)
    note = Note(id=score.allocate_note_id(), **defaults)  # type: ignore[arg-type]
    part.notes.append(note)
    return note


def _seed_project(workspace_dir: Path, score: ScoreIR) -> None:
    storage.write_json(
        storage.score_current_path(workspace_dir, _PROJECT_ID),
        score.model_dump(mode="json"),
    )
    storage.write_json(storage.beatmap_path(workspace_dir, _PROJECT_ID), _BEATMAP)


def _ctx(workspace_dir: Path) -> tools.ToolContext:
    return tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id=_RUN_ID
    )


class TestBuildSdkMcpServer:
    def test_returns_sdk_server_config(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())

        config = sdk_adapter.build_sdk_mcp_server(_ctx(workspace_dir))

        assert config["type"] == "sdk"
        assert config["name"] == sdk_adapter.SERVER_NAME
        assert isinstance(config["instance"], Server)


class TestBuildTools:
    def test_registers_all_eight_tools(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())

        built = sdk_adapter.build_tools(_ctx(workspace_dir))

        assert sorted(t.name for t in built) == sorted(sdk_adapter.TOOL_NAMES)

    async def test_score_context_delegates_to_tools(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        built = {t.name: t for t in sdk_adapter.build_tools(ctx)}

        result = await built["score_context"].handler({})

        assert result["content"][0]["type"] == "text"
        assert json.loads(result["content"][0]["text"]) == tools.score_context(ctx)

    async def test_score_apply_ops_happy_path_writes_staging(
        self, workspace_dir: Path
    ) -> None:
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        built = {t.name: t for t in sdk_adapter.build_tools(ctx)}

        result = await built["score_apply_ops"].handler(
            {
                "ops": [
                    {
                        "type": "note.add",
                        "part_id": "piano",
                        "onset_tick": 960,
                        "duration_tick": 240,
                        "midi": 67,
                    }
                ]
            }
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["ok"] is True
        assert storage.score_staging_path(workspace_dir, _PROJECT_ID, _RUN_ID).exists()

    async def test_score_apply_ops_violation_reported_not_raised(
        self, workspace_dir: Path
    ) -> None:
        """`score_apply_ops`の`{"ok": False, ...}`は正常応答(例外ではない)。"""
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        built = {t.name: t for t in sdk_adapter.build_tools(ctx)}

        result = await built["score_apply_ops"].handler(
            {"ops": [{"type": "note.update", "note_ids": [999999], "midi": 61}]}
        )

        payload = json.loads(result["content"][0]["text"])
        assert payload["ok"] is False
        assert payload["violations"][0]["rule"] == "SCORE_OP_ERROR"

    async def test_score_render_musicxml_returns_plain_string_content(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        _add_note(score, spelling=Spelling(step="C", alter=0, octave=4))
        _seed_project(workspace_dir, score)
        ctx = _ctx(workspace_dir)
        built = {t.name: t for t in sdk_adapter.build_tools(ctx)}

        result = await built["score_render"].handler(
            {"scope": None, "format": "musicxml"}
        )

        text = result["content"][0]["text"]
        assert text.startswith("<?xml")

    async def test_tool_error_propagates_uncaught(self, workspace_dir: Path) -> None:
        """`tools.ToolError`はcatchせず素通しする(module docstring参照)。"""
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        built = {t.name: t for t in sdk_adapter.build_tools(ctx)}

        with pytest.raises(tools.ToolError, match="part not found"):
            await built["score_query"].handler({"part": "nonexistent", "bars": [1, 4]})

    async def test_baseline_diff_raises_tool_error(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        built = {t.name: t for t in sdk_adapter.build_tools(ctx)}

        with pytest.raises(tools.ToolError, match="not implemented"):
            await built["baseline_diff"].handler({})
