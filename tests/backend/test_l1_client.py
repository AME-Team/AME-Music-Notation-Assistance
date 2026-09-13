"""#39: `pipeline/refine/l1_client.py`のテスト。

実際のAnthropic API呼び出しは一切発生しない: `client`は`unittest.mock.Mock()`を
注入し、`client.messages.parse()`の戻り値を差し替えて配線のみを検証する。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import pytest

from app.domain.invariants import Decision
from app.pipeline.refine.l1_chunker import (
    ChunkBars,
    ChunkContext,
    ChunkInput,
    ChunkPart,
)
from app.pipeline.refine.l1_client import L1ChunkResponse, L1ClientError, call_l1_chunk


def _chunk() -> ChunkInput:
    return ChunkInput(
        context=ChunkContext(
            key_estimate="C major",
            key_confidence=0.8,
            time_signature="4/4",
            tempo_bpm=120.0,
            part=ChunkPart(id="piano", instrument="Piano", staves=2),
            chord_hints=[],
            bars=ChunkBars(target=(1, 4), context_before=[], context_after=[5]),
        ),
        notes=[],
    )


def _mock_response(
    *, parsed_output: L1ChunkResponse | None, stop_reason: str = "end_turn"
) -> Mock:
    response = Mock()
    response.stop_reason = stop_reason
    response.parsed_output = parsed_output
    response.usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=10,
    )
    return response


def test_call_l1_chunk_returns_parsed_output_and_usage() -> None:
    parsed = L1ChunkResponse(bar_range=(1, 4), decisions=[])
    client = Mock()
    client.messages.parse.return_value = _mock_response(parsed_output=parsed)

    result = call_l1_chunk(
        _chunk(), client=client, system_prompt="system", song_context="context"
    )

    assert result.output is parsed
    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 10,
    }


def test_call_l1_chunk_passes_output_format_and_effort() -> None:
    client = Mock()
    client.messages.parse.return_value = _mock_response(
        parsed_output=L1ChunkResponse(bar_range=(1, 4), decisions=[])
    )

    call_l1_chunk(
        _chunk(),
        client=client,
        system_prompt="system",
        song_context="context",
        effort="medium",
    )

    _, kwargs = client.messages.parse.call_args
    assert kwargs["output_format"] is L1ChunkResponse
    assert kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_call_l1_chunk_wraps_sdk_errors_as_l1_client_error() -> None:
    """回帰(#39 Gate2レビュー指摘): 以前はrefusal/パース失敗しかL1ClientErrorに

    変換しておらず、`anthropic.AnthropicError`系のSDK例外(レート制限・接続
    エラー等、リトライ後もなお失敗した場合にSDKが送出する)がそのまま
    伝播していた。呼び出し元(l1_runner.py)はL1ClientErrorだけを捕捉して
    チャンク単位で棄却する設計のため、変換されないとリクエスト全体が
    500になってしまう。
    """
    client = Mock()
    client.messages.parse.side_effect = anthropic.RateLimitError(
        "rate limited", response=Mock(status_code=429, headers={}), body=None
    )

    with pytest.raises(L1ClientError):
        call_l1_chunk(
            _chunk(), client=client, system_prompt="system", song_context="context"
        )


def test_call_l1_chunk_raises_on_refusal() -> None:
    client = Mock()
    client.messages.parse.return_value = _mock_response(
        parsed_output=None, stop_reason="refusal"
    )

    with pytest.raises(L1ClientError):
        call_l1_chunk(
            _chunk(), client=client, system_prompt="system", song_context="context"
        )


def test_call_l1_chunk_raises_when_parsed_output_missing() -> None:
    client = Mock()
    client.messages.parse.return_value = _mock_response(
        parsed_output=None, stop_reason="end_turn"
    )

    with pytest.raises(L1ClientError):
        call_l1_chunk(
            _chunk(), client=client, system_prompt="system", song_context="context"
        )


def test_l1_chunk_response_reuses_domain_decision_model() -> None:
    """`decisions`が`domain.invariants.Decision`をそのまま再利用していることの

    回帰確認(検証層と同一スキーマで受け取ることで変換ミスを防ぐ設計)。
    """
    response = L1ChunkResponse(
        bar_range=(1, 4),
        decisions=[Decision(note_id=1, action="delete", reason="test")],
    )
    assert isinstance(response.decisions[0], Decision)
