"""#43: score-mcpコア(`agent/mcp/tools.py`)のテスト。

FastAPI/SDKいずれにも依存しないため、`client`/`async_client`フィクスチャは
使わず、`workspace_dir`のみを使う。`test_l1_diff.py`/`test_score_ops.py`と
同じ流儀で`ScoreIR`を直接組み立て、`storage.write_json`で`current.json`/
`beatmap.json`をディスクへ直接シードする。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent.mcp import tools
from app.domain.score import (
    Note,
    Part,
    ScoreIR,
    SourceInfo,
    TimeSignatureEntry,
)
from app.infra import storage

# test_score_ops.pyと同じ120bpm・4/4のビート列(4小節分)。
_BEATS_120BPM_4_4 = [
    {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
    for i in range(16)
]
_TIME_SIGNATURES_4_4 = [{"bar": 1, "numerator": 4, "denominator": 4}]
_BEATMAP = {"beats": _BEATS_120BPM_4_4, "time_signatures": _TIME_SIGNATURES_4_4}
_PROJECT_ID = "prj_test"
_RUN_ID = "run_test01"


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


def _seed_project(
    workspace_dir: Path, score: ScoreIR, *, with_beatmap: bool = True
) -> None:
    storage.write_json(
        storage.score_current_path(workspace_dir, _PROJECT_ID),
        score.model_dump(mode="json"),
    )
    if with_beatmap:
        storage.write_json(storage.beatmap_path(workspace_dir, _PROJECT_ID), _BEATMAP)


def _ctx(workspace_dir: Path, *, run_id: str = _RUN_ID) -> tools.ToolContext:
    return tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id=run_id
    )


def _staging_path(workspace_dir: Path, *, run_id: str = _RUN_ID) -> Path:
    return storage.score_staging_path(workspace_dir, _PROJECT_ID, run_id)


class TestScoreQuery:
    def test_returns_notes_in_bar_range_with_snap_and_flags(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, midi=60, flags=["ghost_candidate"])
        _add_note(score, onset_tick=1920, midi=64)  # bar 2
        _seed_project(workspace_dir, score)

        result = tools.score_query(_ctx(workspace_dir), part="piano", bars=(1, 1))

        assert len(result) == 1
        assert result[0]["midi"] == 60
        assert result[0]["bar"] == 1
        assert result[0]["flags"] == ["ghost_candidate"]

    def test_filter_narrows_results(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, midi=60, voice=1)
        _add_note(score, onset_tick=240, midi=64, voice=2)
        _seed_project(workspace_dir, score)

        result = tools.score_query(
            _ctx(workspace_dir), part="piano", bars=(1, 4), filter={"voice": 2}
        )

        assert len(result) == 1
        assert result[0]["midi"] == 64

    def test_unknown_part_raises_tool_error(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        with pytest.raises(tools.ToolError, match="part not found"):
            tools.score_query(_ctx(workspace_dir), part="nope", bars=(1, 4))


class TestScoreContext:
    def test_returns_key_time_tempo_chords(self, workspace_dir: Path) -> None:
        score = _make_score()
        _seed_project(workspace_dir, score)

        result = tools.score_context(_ctx(workspace_dir))

        assert result["time_signatures"] == [
            {"bar": 1, "numerator": 4, "denominator": 4}
        ]
        assert result["key_signatures"] == []
        assert result["tempo_map"] == []
        assert result["chords"] == []


class TestScoreStats:
    def test_pitch_range(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, midi=60)
        _add_note(score, onset_tick=240, midi=72)
        _seed_project(workspace_dir, score)

        result = tools.score_stats(
            _ctx(workspace_dir), part="piano", metric="pitch_range"
        )

        assert result == {"min_midi": 60, "max_midi": 72, "mean_midi": 66.0}

    def test_velocity_empty_scope_returns_none(self, workspace_dir: Path) -> None:
        score = _make_score()
        _seed_project(workspace_dir, score)

        result = tools.score_stats(
            _ctx(workspace_dir), part="piano", metric="velocity", bars=(1, 1)
        )

        assert result == {"min": None, "max": None, "mean": None}

    def test_onset_by_bar(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0)
        _add_note(score, onset_tick=240)
        _add_note(score, onset_tick=1920)  # bar 2
        _seed_project(workspace_dir, score)

        result = tools.score_stats(_ctx(workspace_dir), part="piano", metric="onset")

        assert result == {"count": 3, "by_bar": {1: 2, 2: 1}}

    def test_chord_density_with_explicit_bars(self, workspace_dir: Path) -> None:
        score = _make_score()
        _seed_project(workspace_dir, score)

        result = tools.score_stats(
            _ctx(workspace_dir), part="piano", metric="chord_density", bars=(1, 2)
        )

        assert result == {"count": 0, "chords_per_bar": 0.0}


class TestScoreRender:
    def test_stats_format_counts_active_notes_only(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0)
        _add_note(score, onset_tick=240, status="deleted")
        _seed_project(workspace_dir, score)

        result = tools.score_render(_ctx(workspace_dir), scope=None, format="stats")

        assert result["part_count"] == 1
        assert result["note_count"] == 1

    def test_unknown_format_raises(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        with pytest.raises(tools.ToolError, match="unknown format"):
            tools.score_render(_ctx(workspace_dir), scope=None, format="bogus")  # type: ignore[arg-type]


class TestBaselineDiff:
    def test_raises_not_implemented_tool_error(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        with pytest.raises(tools.ToolError, match="not implemented"):
            tools.baseline_diff(_ctx(workspace_dir))


class TestScoreNoteHistory:
    def test_returns_empty_list_when_no_ops_log(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        assert tools.score_note_history(_ctx(workspace_dir), note_id=1) == []

    def test_returns_matching_entries_only(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        ops_log = storage.score_ops_log_path(workspace_dir, _PROJECT_ID)
        storage.append_jsonl(
            ops_log,
            {
                "ops": [{"op": "note.update"}],
                "actor": "user",
                "ts": "2026-01-01T00:00:00Z",
                "changes": {
                    "1": {
                        "part_id": "piano",
                        "before": {"midi": 60},
                        "after": {"midi": 62},
                    }
                },
            },
        )
        storage.append_jsonl(
            ops_log,
            {
                "ops": [{"op": "note.update"}],
                "actor": "user",
                "ts": "2026-01-01T00:01:00Z",
                "changes": {
                    "2": {
                        "part_id": "piano",
                        "before": {"midi": 64},
                        "after": {"midi": 65},
                    }
                },
            },
        )

        history = tools.score_note_history(_ctx(workspace_dir), note_id=1)

        assert len(history) == 1
        assert history[0]["change"]["after"]["midi"] == 62

    def test_malformed_json_line_raises_tool_error(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())
        ops_log = storage.score_ops_log_path(workspace_dir, _PROJECT_ID)
        ops_log.parent.mkdir(parents=True, exist_ok=True)
        ops_log.write_text("{not valid json\n", encoding="utf-8")

        with pytest.raises(tools.ToolError, match="malformed JSON"):
            tools.score_note_history(_ctx(workspace_dir), note_id=1)

    def test_non_dict_changes_field_is_skipped_not_raised(
        self, workspace_dir: Path
    ) -> None:
        _seed_project(workspace_dir, _make_score())
        ops_log = storage.score_ops_log_path(workspace_dir, _PROJECT_ID)
        ops_log.parent.mkdir(parents=True, exist_ok=True)
        with ops_log.open("a", encoding="utf-8") as f:
            f.write(
                '{"ops": [{"op": "ai.reject"}], "actor": "user", '
                '"ts": "2026-01-01T00:00:00Z", "changes": null}\n'
            )

        assert tools.score_note_history(_ctx(workspace_dir), note_id=1) == []


class TestScoreValidate:
    def test_no_violations_on_clean_score(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, duration_tick=240, voice=1)
        _add_note(score, onset_tick=240, duration_tick=240, voice=1)
        _seed_project(workspace_dir, score)

        assert tools.score_validate(_ctx(workspace_dir)) == []

    def test_detects_v8_overlap(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, duration_tick=480, voice=1)
        _add_note(
            score, onset_tick=240, duration_tick=480, voice=1
        )  # overlaps the first
        _seed_project(workspace_dir, score)

        violations = tools.score_validate(_ctx(workspace_dir))

        assert any(v["rule"] == "V-8" for v in violations)

    def test_does_not_flag_different_voices_as_overlapping(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, duration_tick=480, voice=1)
        _add_note(score, onset_tick=240, duration_tick=480, voice=2)
        _seed_project(workspace_dir, score)

        assert tools.score_validate(_ctx(workspace_dir)) == []

    def test_scope_by_part_id(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, duration_tick=480, voice=1)
        _add_note(score, onset_tick=240, duration_tick=480, voice=1)
        _seed_project(workspace_dir, score)

        violations = tools.score_validate(
            _ctx(workspace_dir), scope={"part_id": "other"}
        )

        assert violations == []


class TestScoreApplyOps:
    def test_first_call_initializes_staging_from_current(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, midi=60)
        _seed_project(workspace_dir, score)
        assert not _staging_path(workspace_dir).exists()

        result = tools.score_apply_ops(
            _ctx(workspace_dir),
            ops=[
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 960,
                    "duration_tick": 240,
                    "midi": 67,
                }
            ],
        )

        assert result["ok"] is True
        assert _staging_path(workspace_dir).exists()
        staged = storage.read_json(_staging_path(workspace_dir))
        staged_part = next(p for p in staged["parts"] if p["id"] == "piano")
        assert len(staged_part["notes"]) == 2  # 元の1件 + 追加した1件
        # current.jsonは一切変化していないこと。
        current = storage.read_json(
            storage.score_current_path(workspace_dir, _PROJECT_ID)
        )
        current_part = next(p for p in current["parts"] if p["id"] == "piano")
        assert len(current_part["notes"]) == 1

    def test_second_call_reads_from_staging_not_current(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        _seed_project(workspace_dir, score)
        ctx = _ctx(workspace_dir)

        tools.score_apply_ops(
            ctx,
            ops=[
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 0,
                    "duration_tick": 240,
                    "midi": 60,
                }
            ],
        )
        tools.score_apply_ops(
            ctx,
            ops=[
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 480,
                    "duration_tick": 240,
                    "midi": 64,
                }
            ],
        )

        staged = storage.read_json(_staging_path(workspace_dir))
        staged_part = next(p for p in staged["parts"] if p["id"] == "piano")
        assert sorted(n["midi"] for n in staged_part["notes"]) == [60, 64]

    def test_tags_touched_and_new_notes_with_agent_provenance(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        existing = _add_note(score, onset_tick=0, midi=60)
        untouched = _add_note(score, onset_tick=960, midi=67)
        _seed_project(workspace_dir, score)

        tools.score_apply_ops(
            _ctx(workspace_dir),
            ops=[{"type": "note.update", "note_ids": [existing.id], "midi": 62}],
        )

        staged = storage.read_json(_staging_path(workspace_dir))
        staged_part = next(p for p in staged["parts"] if p["id"] == "piano")
        by_id = {n["id"]: n for n in staged_part["notes"]}
        assert by_id[existing.id]["provenance"] == "agent"
        assert by_id[existing.id]["provenance_run_id"] == _RUN_ID
        assert by_id[untouched.id]["provenance"] == "amt"
        assert by_id[untouched.id]["provenance_run_id"] is None

    def test_aborts_transactionally_on_bad_note_id(self, workspace_dir: Path) -> None:
        score = _make_score()
        note = _add_note(score, onset_tick=0, midi=60)
        _seed_project(workspace_dir, score)

        result = tools.score_apply_ops(
            _ctx(workspace_dir),
            ops=[
                {"type": "note.update", "note_ids": [note.id], "midi": 61},
                {"type": "note.delete", "note_ids": [999999]},
            ],
        )

        assert result["ok"] is False
        assert result["violations"][0]["rule"] == "SCORE_OP_ERROR"
        assert not _staging_path(workspace_dir).exists()

    def test_rejects_malformed_op_without_writing(self, workspace_dir: Path) -> None:
        _seed_project(workspace_dir, _make_score())

        result = tools.score_apply_ops(
            _ctx(workspace_dir), ops=[{"type": "note.bogus"}]
        )

        assert result["ok"] is False
        assert result["violations"][0]["rule"] == "SCORE_OP_ERROR"
        assert not _staging_path(workspace_dir).exists()

    def test_rejects_when_result_introduces_v8_overlap(
        self, workspace_dir: Path
    ) -> None:
        score = _make_score()
        note_a = _add_note(score, onset_tick=0, duration_tick=480, voice=1)
        note_b = _add_note(score, onset_tick=960, duration_tick=480, voice=1)
        _seed_project(workspace_dir, score)

        result = tools.score_apply_ops(
            _ctx(workspace_dir),
            ops=[{"type": "note.update", "note_ids": [note_b.id], "onset_tick": 240}],
        )

        assert result["ok"] is False
        assert any(v["rule"] == "V-8" for v in result["violations"])
        assert not _staging_path(workspace_dir).exists()
        # note_aへ触れていないので、note_aは元のprovenanceのまま(current.json自体も無変更)。
        current = storage.read_json(
            storage.score_current_path(workspace_dir, _PROJECT_ID)
        )
        current_part = next(p for p in current["parts"] if p["id"] == "piano")
        by_id = {n["id"]: n for n in current_part["notes"]}
        assert by_id[note_a.id]["provenance"] == "amt"

    def test_missing_beatmap_raises_tool_error(self, workspace_dir: Path) -> None:
        score = _make_score()
        _add_note(score, onset_tick=0, midi=60)
        _seed_project(workspace_dir, score, with_beatmap=False)

        with pytest.raises(tools.ToolError, match="beatmap not found"):
            tools.score_apply_ops(
                _ctx(workspace_dir),
                ops=[{"type": "note.update", "note_ids": [1], "midi": 61}],
            )

    def test_preexisting_unrelated_violation_does_not_block_unrelated_edit(
        self, workspace_dir: Path
    ) -> None:
        """#43 Gate2レビュー指摘: AMT/quantize由来の、このrunと無関係な既存V-8

        違反(同一voice内重複)が1つでもあると、それを直さない限りエージェントの
        一切の編集が通らなくなる問題を修正した(pre/postの違反差分のみを見る)。
        既存の重複とは無関係な小節へのノート追加は成功しなければならない。
        """
        score = _make_score()
        # bar 1に、このrunとは無関係な既存のV-8重複(voice=1)を意図的に作る。
        _add_note(score, onset_tick=0, duration_tick=480, voice=1, provenance="amt")
        _add_note(score, onset_tick=240, duration_tick=480, voice=1, provenance="amt")
        _seed_project(workspace_dir, score)

        # bar 2への無関係なノート追加は、既存の重複を解消しなくても成功するべき。
        result = tools.score_apply_ops(
            _ctx(workspace_dir),
            ops=[
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 1920,
                    "duration_tick": 240,
                    "midi": 67,
                }
            ],
        )

        assert result["ok"] is True
        staged = storage.read_json(_staging_path(workspace_dir))
        staged_part = next(p for p in staged["parts"] if p["id"] == "piano")
        assert len(staged_part["notes"]) == 3

    def test_edit_that_worsens_preexisting_violation_is_still_rejected(
        self, workspace_dir: Path
    ) -> None:
        """既存の重複を許容するのは「無関係」な場合のみ。この`apply_ops`自体が

        (別の)新たなV-8重複を作る場合は、既存の重複の有無に関わらず拒否する。
        """
        score = _make_score()
        _add_note(score, onset_tick=0, duration_tick=480, voice=1, provenance="amt")
        _add_note(score, onset_tick=240, duration_tick=480, voice=1, provenance="amt")
        other_note = _add_note(
            score, onset_tick=0, duration_tick=480, voice=2, provenance="amt"
        )
        _seed_project(workspace_dir, score)

        # voice=2のノートをvoice=1(既存の重複ペアと同じ小節)へ動かし、新規の重複を作る。
        result = tools.score_apply_ops(
            _ctx(workspace_dir),
            ops=[{"type": "note.update", "note_ids": [other_note.id], "voice": 1}],
        )

        assert result["ok"] is False
        assert any(
            v["rule"] == "V-8" and v["note_id"] == other_note.id
            for v in result["violations"]
        )
        assert not _staging_path(workspace_dir).exists()
