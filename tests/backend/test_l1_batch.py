"""#40: L1 Batchオーケストレーション(`app.pipeline.refine.l1_batch`)のテスト。

Anthropic Batch APIをモックし、リクエスト形状、ポーリング待機、結果回収、
検証層連携、ステージングへの適用、エラー処理を網羅的に検証する。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
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
from app.pipeline.refine.l1_client import L1ClientError


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


def test_run_l1_batch_success():
    score = _make_test_score()
    client = MagicMock()

    # create
    batch_mock = SimpleNamespace(id="msgbatch_123")
    client.messages.batches.create.return_value = batch_mock

    # retrieve (in_progress -> ended)
    client.messages.batches.retrieve.side_effect = [
        SimpleNamespace(processing_status="in_progress"),
        SimpleNamespace(processing_status="ended"),
    ]

    # results
    mock_item = SimpleNamespace(
        custom_id="chunk_0",
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"bar_range": [1, 1], "decisions": [{"note_id": 101, "action": "keep", "snap": "a", "spelling": {"step": "C", "alter": 0, "octave": 4}, "voice": 1, "staff": 1, "tie": {"start": false, "stop": false}, "reason": "tonic"}], "bar_annotations": []}',
                    )
                ],
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=500,
                    cache_read_input_tokens=200,
                    cache_creation_input_tokens=100,
                ),
            ),
        ),
    )
    client.messages.batches.results.return_value = [mock_item]

    result = run_l1_batch(
        score,
        "piano",
        client=client,
        run_id="run_batch_001",
        beat_anchors=_make_beat_anchors(),
        poll_interval_sec=0.01,
        max_poll_time_sec=2.0,
    )

    assert result.chunks_ok == 1
    assert result.chunks_rejected == 0
    assert result.usage["input_tokens"] == 1000
    assert result.usage["output_tokens"] == 500
    assert result.usage["cache_read_input_tokens"] == 200
    assert result.usage["cache_creation_input_tokens"] == 100

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
    assert refine_meta["l1"]["cost_usd"] > 0


def test_run_l1_batch_timeout():
    score = _make_test_score()
    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="msgbatch_timeout")
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="in_progress"
    )

    with pytest.raises(L1ClientError, match="timed out"):
        run_l1_batch(
            score,
            "piano",
            client=client,
            run_id="run_batch_timeout",
            beat_anchors=_make_beat_anchors(),
            poll_interval_sec=0.01,
            max_poll_time_sec=0.05,
        )


def test_run_l1_batch_chunk_error():
    score = _make_test_score()
    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="msgbatch_err")
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended"
    )

    mock_err_item = SimpleNamespace(
        custom_id="chunk_0",
        result=SimpleNamespace(
            type="errored",
            error=SimpleNamespace(message="Rate limit exceeded"),
        ),
    )
    client.messages.batches.results.return_value = [mock_err_item]

    result = run_l1_batch(
        score,
        "piano",
        client=client,
        run_id="run_batch_err",
        beat_anchors=_make_beat_anchors(),
        poll_interval_sec=0.01,
    )

    assert result.chunks_ok == 0
    assert result.chunks_rejected == 1
    assert len(result.rejected_reasons) == 1
    assert "batch error" in result.rejected_reasons[0]


def test_run_l1_batch_invariant_violation():
    score = _make_test_score()
    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="msgbatch_inv")
    client.messages.batches.retrieve.return_value = SimpleNamespace(
        processing_status="ended"
    )

    # snap='nonexistent' violates V-3
    mock_item = SimpleNamespace(
        custom_id="chunk_0",
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"bar_range": [1, 1], "decisions": [{"note_id": 101, "action": "keep", "snap": "nonexistent", "spelling": {"step": "C", "alter": 0, "octave": 4}, "voice": 1, "staff": 1, "tie": {"start": false, "stop": false}, "reason": "bad snap"}], "bar_annotations": []}',
                    )
                ],
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=500,
                    cache_read_input_tokens=0,
                ),
            ),
        ),
    )
    client.messages.batches.results.return_value = [mock_item]

    result = run_l1_batch(
        score,
        "piano",
        client=client,
        run_id="run_batch_inv",
        beat_anchors=_make_beat_anchors(),
        poll_interval_sec=0.01,
    )

    assert result.chunks_ok == 0
    assert result.chunks_rejected == 1
    assert len(result.rejected_reasons) == 1
    assert "V-3" in result.rejected_reasons[0]


def test_run_l1_batch_unknown_part():
    score = _make_test_score()
    client = MagicMock()

    with pytest.raises(ValueError, match="part 'guitar' not found"):
        run_l1_batch(
            score,
            "guitar",
            client=client,
            run_id="run_001",
            beat_anchors=_make_beat_anchors(),
        )
