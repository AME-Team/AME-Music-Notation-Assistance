"""#104: L1 Batch(並列claude CLI呼び出し)オーケストレーション(`app.pipeline.refine.l1_batch`)のテスト。

`call_l1_chunk`(実際のCLI呼び出しを行う関数)は常にモックし、実プロセスは一切
起動しない。並列実行後の検証層連携・ステージングへの適用・エラー処理を検証する。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.domain.invariants import Decision
from app.domain.score import (
    Clef,
    Note,
    Part,
    ScoreIR,
    SnapCandidate,
    SourceInfo,
    Spelling,
    TempoMapEntry,
    TimeSignatureEntry,
)
from app.pipeline.refine.l1_batch import run_l1_batch
from app.pipeline.refine.l1_client import (
    L1ChunkCallResult,
    L1ChunkResponse,
    L1ClientError,
)


def _make_test_score() -> ScoreIR:
    score = ScoreIR(
        project_id="prj_test",
        source=SourceInfo(filename="test.wav", duration_sec=4.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        tempo_map=[TempoMapEntry(bar=1, beat=1.0, bpm=120.0)],
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )
    part.notes.append(
        Note(
            id=101,
            onset_sec=0.0,
            duration_sec=0.5,
            onset_tick=0,
            duration_tick=480,
            midi=60,
            velocity=80,
            provenance="amt",
            voice=1,
            staff=1,
            spelling=Spelling(step="C", alter=0, octave=4),
            snap_candidates=[
                SnapCandidate(id="a", resolution="1/8", tick=0, score=1.0),
                SnapCandidate(id="b", resolution="1/16", tick=120, score=0.5),
            ],
        )
    )
    score.parts.append(part)
    return score


def _make_beat_anchors() -> list[tuple[float, float]]:
    return [(i * 0.5, float(i * 480)) for i in range(16)]


def _mock_calls(*results):
    """`call_l1_chunk`を`side_effect`でチャンク順に差し替える(#104)。

    `L1ClientError`インスタンスを渡すとそのチャンクは棄却扱いになる
    (`_call_chunk`が例外を握り潰さず値として返す設計、`l1_batch.py`参照)。
    """

    def _side_effect(chunk, **kwargs):
        result = results[_side_effect.calls]
        _side_effect.calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    _side_effect.calls = 0
    return patch("app.pipeline.refine.l1_batch.call_l1_chunk", side_effect=_side_effect)


def test_run_l1_batch_success():
    score = _make_test_score()
    decision = Decision(
        note_id=101,
        action="keep",
        snap="a",
        spelling=Spelling(step="C", alter=0, octave=4),
        voice=1,
        staff=1,
        reason="tonic",
    )
    call_result = L1ChunkCallResult(
        output=L1ChunkResponse(bar_range=[1, 1], decisions=[decision]),
        usage={
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 100,
        },
        cost_usd=0.042,
    )

    with _mock_calls(call_result):
        result = run_l1_batch(
            score,
            "piano",
            run_id="run_batch_001",
            beat_anchors=_make_beat_anchors(),
        )

    assert result.chunks_ok == 1
    assert result.chunks_rejected == 0
    assert result.usage["input_tokens"] == 1000
    assert result.usage["output_tokens"] == 500
    assert result.usage["cache_read_input_tokens"] == 200
    assert result.usage["cache_creation_input_tokens"] == 100
    assert result.cost_usd == 0.042

    # ステージングScoreIRの確認
    part = result.staged_score.find_part("piano")
    assert part is not None
    note = part.notes[0]
    assert note.provenance == "llm"
    assert note.provenance_run_id == "run_batch_001"
    assert note.spelling.step == "C"

    # メタデータの確認
    refine_meta = result.staged_score.meta.stages["refine"]
    assert refine_meta["l1"]["mode"] == "batch"
    assert refine_meta["l1"]["chunks_ok"] == 1
    assert refine_meta["l1"]["cost_usd"] == 0.042


def test_run_l1_batch_chunk_error():
    score = _make_test_score()

    with _mock_calls(L1ClientError("rate limited")):
        result = run_l1_batch(
            score,
            "piano",
            run_id="run_batch_err",
            beat_anchors=_make_beat_anchors(),
        )

    assert result.chunks_ok == 0
    assert result.chunks_rejected == 1
    assert len(result.rejected_reasons) == 1
    assert "rate limited" in result.rejected_reasons[0]


def test_run_l1_batch_invariant_violation():
    score = _make_test_score()
    # snap='nonexistent' はV-3(snap候補チェック)に違反する。
    bad_decision = Decision(
        note_id=101,
        action="keep",
        snap="nonexistent",
        spelling=Spelling(step="C", alter=0, octave=4),
        voice=1,
        staff=1,
        reason="bad snap",
    )
    call_result = L1ChunkCallResult(
        output=L1ChunkResponse(bar_range=[1, 1], decisions=[bad_decision]),
        usage={
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        cost_usd=0.01,
    )

    with _mock_calls(call_result):
        result = run_l1_batch(
            score,
            "piano",
            run_id="run_batch_inv",
            beat_anchors=_make_beat_anchors(),
        )

    assert result.chunks_ok == 0
    assert result.chunks_rejected == 1
    assert len(result.rejected_reasons) == 1
    assert "V-3" in result.rejected_reasons[0]


def test_run_l1_batch_unknown_part():
    score = _make_test_score()

    with pytest.raises(ValueError, match="part 'guitar' not found"):
        run_l1_batch(
            score,
            "guitar",
            run_id="run_001",
            beat_anchors=_make_beat_anchors(),
        )


def test_run_l1_batch_no_chunks_returns_empty_result():
    """パートにノートが無い(=チャンクが0件)場合、CLI呼び出し無しで即座に返る。"""
    score = ScoreIR(
        project_id="prj_empty",
        source=SourceInfo(filename="empty.wav", duration_sec=1.0, sample_rate=8000),
        divisions=480,
    )
    score.parts.append(
        Part(id="piano", name="Piano", midi_program=0, staves=1, clefs=[], notes=[])
    )

    with patch("app.pipeline.refine.l1_batch.call_l1_chunk") as mock_call:
        result = run_l1_batch(
            score, "piano", run_id="run_empty", beat_anchors=_make_beat_anchors()
        )

    mock_call.assert_not_called()
    assert result.chunks_ok == 0
    assert result.chunks_rejected == 0
