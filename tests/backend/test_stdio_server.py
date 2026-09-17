"""#44: score-mcp stdioアダプタ(`agent/mcp/stdio_server.py`)のテスト。

`FastMCP.call_tool`(直接呼び出し用の簡易メソッド)はワイヤープロトコル層を
経由しないため、`tools.ToolError`は`mcp.server.fastmcp.exceptions.ToolError`
として再送出される(実プロセス起動時の挙動とは異なる、module docstring参照)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from app.agent.mcp import stdio_server, tools
from app.domain.score import Note, Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import storage
from mcp.server.fastmcp import FastMCP

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


class TestBuildServer:
    def test_returns_fastmcp_instance(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())

        server = stdio_server.build_server(_ctx(workspace_dir))

        assert isinstance(server, FastMCP)

    async def test_registers_all_eight_tools(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        server = stdio_server.build_server(_ctx(workspace_dir))

        listed = await server.list_tools()

        assert sorted(t.name for t in listed) == sorted(
            [
                "score_query",
                "score_context",
                "score_stats",
                "score_validate",
                "score_render",
                "baseline_diff",
                "score_note_history",
                "score_apply_ops",
            ]
        )

    async def test_score_context_delegates_to_tools(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        server = stdio_server.build_server(ctx)

        _, structured = await server.call_tool("score_context", {})

        assert structured == tools.score_context(ctx)

    async def test_score_apply_ops_happy_path_writes_staging(
        self, workspace_dir: Path
    ) -> None:
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        server = stdio_server.build_server(ctx)

        _, structured = await server.call_tool(
            "score_apply_ops",
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
            },
        )

        assert structured["ok"] is True
        assert storage.score_staging_path(workspace_dir, _PROJECT_ID, _RUN_ID).exists()

    async def test_tool_error_message_propagates(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        ctx = _ctx(workspace_dir)
        server = stdio_server.build_server(ctx)

        with pytest.raises(Exception, match="part not found") as exc_info:
            await server.call_tool(
                "score_query", {"part": "nonexistent", "bars": [1, 4]}
            )
        assert "nonexistent" in str(exc_info.value)


class TestOpencodeMcpConfig:
    def test_matches_design_doc_shape(self, workspace_dir: Path) -> None:
        config = stdio_server.opencode_mcp_config(
            project_id="prj_01H", run_id="run_01H", workspace_dir=workspace_dir
        )

        assert config == {
            "score": {
                "type": "local",
                "command": [sys.executable, "-m", "app.agent.mcp.stdio_server"],
                "environment": {
                    "AME_WORKSPACE_DIR": str(workspace_dir),
                    "AME_PROJECT_ID": "prj_01H",
                    "AME_RUN_ID": "run_01H",
                },
                "enabled": True,
                "timeout": 30000,
            }
        }


class TestContextFromEnv:
    def test_builds_context_from_env_vars(
        self, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AME_WORKSPACE_DIR", str(workspace_dir))
        monkeypatch.setenv("AME_PROJECT_ID", "prj_env")
        monkeypatch.setenv("AME_RUN_ID", "run_env")

        ctx = stdio_server.context_from_env()

        assert ctx == tools.ToolContext(
            workspace_dir=workspace_dir, project_id="prj_env", run_id="run_env"
        )

    def test_missing_env_vars_raise_runtime_error_naming_all_missing_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AME_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AME_PROJECT_ID", raising=False)
        monkeypatch.delenv("AME_RUN_ID", raising=False)

        with pytest.raises(
            RuntimeError, match="AME_WORKSPACE_DIR.*AME_PROJECT_ID.*AME_RUN_ID"
        ):
            stdio_server.context_from_env()

    def test_partially_missing_env_var_names_only_the_missing_one(
        self, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AME_WORKSPACE_DIR", str(workspace_dir))
        monkeypatch.setenv("AME_PROJECT_ID", "prj_env")
        monkeypatch.delenv("AME_RUN_ID", raising=False)

        with pytest.raises(RuntimeError, match="AME_RUN_ID") as exc_info:
            stdio_server.context_from_env()
        assert "AME_WORKSPACE_DIR" not in str(exc_info.value)
        assert "AME_PROJECT_ID" not in str(exc_info.value)
