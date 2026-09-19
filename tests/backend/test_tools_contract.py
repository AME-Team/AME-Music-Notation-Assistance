"""#53: tools.py に対するプロバイダ非依存の契約テスト。

score-mcp コア (`backend/app/agent/mcp/tools.py`) が満たすべき契約を検証する:
1. プロバイダ非依存性: FastMCP / Claude SDK いずれにも依存せず純粋な Python 関数として動作する。
2. 読み取り専用ツールの不変性: current.json / staging のいずれも変更しない。素の dict/list を返す。
3. 書き込みツールの単一性・ステージング隔離性:
   - `score_apply_ops` が唯一の書き込み経路。
   - `current.json` を直接変更せず、`score/staging/{run_id}.json` のみに書き込む。
   - 同一 run 内で複数回呼んだ場合、前回のステージングを読み継いで累積適用される。
   - 異なる run_id 間でステージングが完全に独立し、干渉しない。
4. 検証不変条件 (Invariants) 違反の扱い:
   - 違反時は例外を投げず、`{"ok": False, "violations": [...]}` を返し、ステージングを汚染しない。
5. 前提条件エラーの扱い:
   - 存在しないプロジェクトや破損ファイルに対しては一貫して `ToolError` を送出する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agent.mcp import tools
from app.domain.score import Note, Part, ScoreIR, SourceInfo, TimeSignatureEntry
from app.infra import storage

_PROJECT_ID = "prj_contract"
_RUN_ID_1 = "run_alpha"
_RUN_ID_2 = "run_beta"

_BEATS_120BPM_4_4 = [
    {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
    for i in range(16)
]
_TIME_SIGNATURES_4_4 = [{"bar": 1, "numerator": 4, "denominator": 4}]
_BEATMAP = {"beats": _BEATS_120BPM_4_4, "time_signatures": _TIME_SIGNATURES_4_4}


def _make_score() -> ScoreIR:
    score = ScoreIR(
        project_id=_PROJECT_ID,
        source=SourceInfo(filename="test.wav", duration_sec=8.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[Part(id="piano", name="Piano", midi_program=0, staves=2)],
    )
    part = score.find_part("piano")
    assert part is not None
    # 2小節分のノートを配置 (小節1: C4, 小節2: E4)
    part.notes.extend(
        [
            Note(
                id=score.allocate_note_id(),
                onset_sec=0.0,
                duration_sec=1.0,
                onset_tick=0,
                duration_tick=960,
                midi=60,
                velocity=80,
                provenance="amt",
                voice=1,
                staff=1,
            ),
            Note(
                id=score.allocate_note_id(),
                onset_sec=2.0,
                duration_sec=1.0,
                onset_tick=1920,
                duration_tick=960,
                midi=64,
                velocity=85,
                provenance="amt",
                voice=1,
                staff=1,
            ),
        ]
    )
    return score


def _seed(workspace_dir: Path) -> ScoreIR:
    score = _make_score()
    storage.write_json(
        storage.score_current_path(workspace_dir, _PROJECT_ID),
        score.model_dump(mode="json"),
    )
    storage.write_json(storage.beatmap_path(workspace_dir, _PROJECT_ID), _BEATMAP)
    return score


def _ctx(workspace_dir: Path, run_id: str = _RUN_ID_1) -> tools.ToolContext:
    return tools.ToolContext(
        workspace_dir=workspace_dir, project_id=_PROJECT_ID, run_id=run_id
    )


# =========================================================================
# 1. 読み取り専用ツールの契約テスト
# =========================================================================


def test_read_only_tools_contract_return_types_and_immutability(
    workspace_dir: Path,
) -> None:
    """すべての読み取り専用ツールが JSON シリアライズ可能な素の型を返し、

    ディスク上のファイルを一切変更しないことを検証する。
    """
    _seed(workspace_dir)
    ctx = _ctx(workspace_dir)
    current_path = storage.score_current_path(workspace_dir, _PROJECT_ID)
    current_mtime_before = current_path.stat().st_mtime_ns
    current_content_before = current_path.read_text(encoding="utf-8")

    # 1. score_context
    ctx_res = tools.score_context(ctx)
    assert isinstance(ctx_res, dict)
    assert "key_signatures" in ctx_res
    assert "time_signatures" in ctx_res
    json.dumps(ctx_res)  # JSON シリアライズ可能

    # 2. score_query
    query_res = tools.score_query(ctx, part="piano", bars=(1, 2))
    assert isinstance(query_res, list)
    assert len(query_res) == 2
    assert query_res[0]["id"] == 1
    assert query_res[1]["id"] == 2
    json.dumps(query_res)

    # 3. score_stats
    stats_res = tools.score_stats(ctx, part="piano", metric="pitch_range")
    assert isinstance(stats_res, dict)
    assert stats_res["min_midi"] == 60
    assert stats_res["max_midi"] == 64
    json.dumps(stats_res)

    # 4. score_validate
    val_res = tools.score_validate(ctx)
    assert isinstance(val_res, list)  # list of violation dicts
    json.dumps(val_res)

    # 5. score_render
    render_res = tools.score_render(ctx, scope=None, format="stats")
    assert isinstance(render_res, dict)
    assert render_res["note_count"] == 2
    json.dumps(render_res)

    # 6. score_note_history
    history_res = tools.score_note_history(ctx, note_id=1)
    assert isinstance(history_res, list)
    json.dumps(history_res)

    # 検証: current.json が一切変更されていないこと
    assert current_path.stat().st_mtime_ns == current_mtime_before
    assert current_path.read_text(encoding="utf-8") == current_content_before

    # 検証: staging ディレクトリ/ファイルが作成されていないこと
    staging_path = storage.score_staging_path(workspace_dir, _PROJECT_ID, _RUN_ID_1)
    assert not staging_path.exists()


# =========================================================================
# 2. 書き込みツールの契約テスト (score_apply_ops)
# =========================================================================


def test_score_apply_ops_writes_only_to_staging(workspace_dir: Path) -> None:
    """`score_apply_ops` は唯一の書き込み経路であり、current.json を変更せず

    `score/staging/{run_id}.json` のみに書き込むことを検証する。
    """
    _seed(workspace_dir)
    ctx = _ctx(workspace_dir, _RUN_ID_1)
    current_path = storage.score_current_path(workspace_dir, _PROJECT_ID)
    current_content_before = current_path.read_text(encoding="utf-8")
    staging_path = storage.score_staging_path(workspace_dir, _PROJECT_ID, _RUN_ID_1)

    # ノート追加操作 (小節1の空きスペース: tick 960〜1440)
    op = {
        "type": "note.add",
        "part_id": "piano",
        "onset_tick": 960,
        "duration_tick": 480,
        "midi": 62,
        "velocity": 88,
    }
    result = tools.score_apply_ops(ctx, ops=[op])

    assert result["ok"] is True
    assert result["run_id"] == _RUN_ID_1

    # current.json は一切変更されていない
    assert current_path.read_text(encoding="utf-8") == current_content_before

    # staging ファイルに反映されている
    assert staging_path.exists()
    staged_data = storage.read_json(staging_path)
    part_notes = staged_data["parts"][0]["notes"]
    assert len(part_notes) == 3
    assert any(n["midi"] == 62 for n in part_notes)


def test_score_apply_ops_staging_accumulation_and_read_visibility(
    workspace_dir: Path,
) -> None:
    """同一 run 内で複数回 `score_apply_ops` を呼んだ場合、前回のステージングを

    読み継いで累積適用され、読み取り専用ツールからもその変更が可視になることを検証する。
    """
    _seed(workspace_dir)
    ctx = _ctx(workspace_dir, _RUN_ID_1)

    # 1回目の変更: ノート1の voice を 2 に変更
    res1 = tools.score_apply_ops(
        ctx,
        ops=[{"type": "note.update", "note_ids": [1], "voice": 2}],
    )
    assert res1["ok"] is True

    # 読み取りツールがステージングを読み継いでいること
    q1 = tools.score_query(ctx, part="piano", bars=(1, 1))
    assert q1[0]["voice"] == 2

    # 2回目の変更: ノート2の staff を 2 に変更
    res2 = tools.score_apply_ops(
        ctx,
        ops=[{"type": "note.update", "note_ids": [2], "staff": 2}],
    )
    assert res2["ok"] is True

    # 両方の変更が累積されていること
    q_all = tools.score_query(ctx, part="piano", bars=(1, 2))
    n1 = next(n for n in q_all if n["id"] == 1)
    n2 = next(n for n in q_all if n["id"] == 2)
    assert n1["voice"] == 2
    assert n2["staff"] == 2


def test_staging_isolation_between_different_runs(workspace_dir: Path) -> None:
    """異なる run_id 間でステージングが完全に独立し、干渉しないことを検証する。"""
    _seed(workspace_dir)
    ctx_1 = _ctx(workspace_dir, _RUN_ID_1)
    ctx_2 = _ctx(workspace_dir, _RUN_ID_2)

    # Run 1 でノート追加 (tick 960〜1440)
    tools.score_apply_ops(
        ctx_1,
        ops=[
            {
                "type": "note.add",
                "part_id": "piano",
                "onset_tick": 960,
                "duration_tick": 480,
                "midi": 72,
            }
        ],
    )

    # Run 2 からは Run 1 の変更が見えず、current.json の状態が見える
    q2 = tools.score_query(ctx_2, part="piano", bars=(1, 2))
    assert len(q2) == 2
    assert all(n["midi"] != 72 for n in q2)

    # Run 2 で別の変更を行う
    tools.score_apply_ops(
        ctx_2,
        ops=[
            {
                "type": "note.add",
                "part_id": "piano",
                "onset_tick": 960,
                "duration_tick": 480,
                "midi": 74,
            }
        ],
    )

    # Run 1 は 72 を含み、Run 2 は 74 を含む
    q1_after = tools.score_query(ctx_1, part="piano", bars=(1, 2))
    q2_after = tools.score_query(ctx_2, part="piano", bars=(1, 2))
    assert any(n["midi"] == 72 for n in q1_after)
    assert not any(n["midi"] == 74 for n in q1_after)
    assert any(n["midi"] == 74 for n in q2_after)
    assert not any(n["midi"] == 72 for n in q2_after)


# =========================================================================
# 3. 検証不変条件 (Invariants) 違反と拒否の契約
# =========================================================================


def test_score_apply_ops_invariant_violations_are_rejected_safely(
    workspace_dir: Path,
) -> None:
    """不変条件違反 (同一 voice 内の時間重複 V-8) を引き起こす ops は

    例外を投げず、`ok=False` と `violations` リストを返し、ステージングを汚染しない。
    """
    _seed(workspace_dir)
    ctx = _ctx(workspace_dir)
    staging_path = storage.score_staging_path(workspace_dir, _PROJECT_ID, _RUN_ID_1)

    # ノート1 (tick 0-960, voice 1) と完全に重複するノートを同一 voice 1 に追加
    conflicting_op = {
        "type": "note.add",
        "part_id": "piano",
        "onset_tick": 0,
        "duration_tick": 480,
        "midi": 67,
        "voice": 1,
        "staff": 1,
    }

    result = tools.score_apply_ops(ctx, ops=[conflicting_op])

    assert result["ok"] is False
    assert "violations" in result
    assert len(result["violations"]) > 0
    assert any(v.get("rule") == "V-8" for v in result["violations"])

    # 失敗時、ステージングファイルは作成されない
    assert not staging_path.exists()


# =========================================================================
# 4. 前提条件エラー (ToolError) の契約
# =========================================================================


def test_tool_error_raised_for_missing_project(workspace_dir: Path) -> None:
    """存在しないプロジェクトに対してツールを呼んだ場合、一貫して `ToolError` を送出する。"""
    ctx = tools.ToolContext(
        workspace_dir=workspace_dir, project_id="non_existent", run_id=_RUN_ID_1
    )

    with pytest.raises(tools.ToolError, match="failed to read score"):
        tools.score_context(ctx)

    with pytest.raises(tools.ToolError):
        tools.score_query(ctx, part="piano", bars=(1, 2))

    with pytest.raises(tools.ToolError):
        tools.score_apply_ops(
            ctx,
            ops=[
                {
                    "type": "note.add",
                    "part_id": "piano",
                    "onset_tick": 0,
                    "duration_tick": 480,
                    "midi": 60,
                }
            ],
        )


def test_baseline_diff_raises_tool_error_consistently(workspace_dir: Path) -> None:
    """`baseline_diff` は未実装ツールとして一貫して `ToolError` を送出する。"""
    _seed(workspace_dir)
    ctx = _ctx(workspace_dir)
    with pytest.raises(tools.ToolError, match="not implemented"):
        tools.baseline_diff(ctx)
