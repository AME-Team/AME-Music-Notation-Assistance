"""L1: Anthropic Messages Batches APIによる一括オーケストレーション(#40, 設計書§7.3/§7.5/R-9)。

- R-9: Batch API (50%割引) を活用し、曲全体の全チャンクをまとめて非同期バッチ処理。
- 結果回収後に検証層(V-1〜V-9)を通し、合格したチャンクのみステージングScoreIRへ適用。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import anthropic

from app.domain.invariants import validate_decisions
from app.pipeline.refine.cost import calculate_usage_cost_usd
from app.pipeline.refine.l1_chunker import build_chunks
from app.pipeline.refine.l1_client import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    MAX_TOKENS,
    L1ChunkResponse,
    L1ClientError,
)
from app.pipeline.refine.l1_prompt import (
    build_chunk_message,
    build_song_context_message,
    build_system_prompt,
)
from app.pipeline.refine.l1_runner import (
    _UNSUPPORTED_ACTIONS,
    L1RunResult,
    _apply_delete,
    _apply_keep,
    _apply_split_tie,
    _validation_notes_for_chunk,
)
from app.pipeline.time_signature import tick_to_bar_beat

if TYPE_CHECKING:
    from app.domain.score import ScoreIR


def _parse_batch_message_content(message: Any, target_bars: tuple[int, int]) -> L1ChunkResponse:
    """バッチメッセージから`L1ChunkResponse`を取り出す。"""
    if getattr(message, "stop_reason", None) == "refusal":
        raise L1ClientError(f"model refused chunk {target_bars}")

    text_parts = [
        block.text
        for block in getattr(message, "content", [])
        if getattr(block, "type", None) == "text" or hasattr(block, "text")
    ]
    if not text_parts:
        raise L1ClientError(f"empty content in batch response for chunk {target_bars}")

    raw_json = "".join(text_parts)
    try:
        return L1ChunkResponse.model_validate_json(raw_json)
    except Exception as exc:
        raise L1ClientError(
            f"failed to parse structured output for chunk {target_bars}: {exc}"
        ) from exc


def run_l1_batch(
    score: ScoreIR,
    part_id: str,
    *,
    client: anthropic.Anthropic,
    run_id: str,
    beat_anchors: list[tuple[float, float]],
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    poll_interval_sec: float = 0.5,
    max_poll_time_sec: float = 600.0,
) -> L1RunResult:
    """1パートを対象にAnthropic Batch APIでL1を実行し、ステージング用ScoreIRを返す。

    `score`自体は変更せず複製(deep copy)に対して操作する。
    全チャンクを1つのメッセージバッチとして作成・投入し、完了をポーリングして
    回収後、検証層(V-1〜V-9)を通してステージングへ適用する。
    """
    if score.find_part(part_id) is None:
        raise ValueError(f"part {part_id!r} not found in score")

    staged = score.model_copy(deep=True)
    part = staged.find_part(part_id)
    assert part is not None
    notes_by_id = {n.id: n for n in part.notes}

    chunks = build_chunks(score, part_id)
    if not chunks:
        return L1RunResult(
            staged_score=staged,
            chunks_ok=0,
            chunks_rejected=0,
            rejected_reasons=[],
            skipped_decisions=[],
            usage={"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
        )

    system_prompt = build_system_prompt()
    song_context = build_song_context_message(score, part_id)
    time_signatures_raw = [ts.model_dump(mode="json") for ts in staged.time_signatures]

    # バッチリクエストの構築
    json_schema = L1ChunkResponse.model_json_schema()
    batch_requests = []
    chunk_map = {}
    for idx, chunk in enumerate(chunks):
        custom_id = f"chunk_{idx}"
        chunk_map[custom_id] = chunk
        batch_requests.append(
            {
                "custom_id": custom_id,
                "params": {
                    "model": model,
                    "max_tokens": MAX_TOKENS,
                    "system": [
                        {
                            "type": "text",
                            "text": system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": song_context,
                                    "cache_control": {"type": "ephemeral"},
                                },
                                {"type": "text", "text": build_chunk_message(chunk)},
                            ],
                        }
                    ],
                    "output_config": {
                        "effort": effort,
                        "format": {
                            "type": "json_schema",
                            "schema": json_schema,
                        },
                    },
                    "thinking": {"type": "adaptive"},
                },
            }
        )

    # バッチ作成
    try:
        batch = client.messages.batches.create(requests=batch_requests)  # type: ignore[arg-type]
    except anthropic.AnthropicError as exc:
        raise L1ClientError(f"Anthropic batch creation failed: {exc}") from exc

    # ポーリング待機
    start_time = time.monotonic()
    batch_id = batch.id
    while True:
        try:
            current_batch = client.messages.batches.retrieve(batch_id)
        except anthropic.AnthropicError as exc:
            raise L1ClientError(f"Anthropic batch retrieve failed: {exc}") from exc

        if current_batch.processing_status == "ended":
            break

        if time.monotonic() - start_time > max_poll_time_sec:
            raise L1ClientError(f"Anthropic batch {batch_id} timed out after {max_poll_time_sec}s")

        time.sleep(poll_interval_sec)

    # 結果回収
    try:
        results = client.messages.batches.results(batch_id)
    except anthropic.AnthropicError as exc:
        raise L1ClientError(f"Anthropic batch results retrieval failed: {exc}") from exc

    chunks_ok = 0
    chunks_rejected = 0
    rejected_reasons: list[str] = []
    skipped_decisions: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    for item in results:
        custom_id = item.custom_id
        target_chunk = chunk_map.get(custom_id)
        if target_chunk is None:
            continue

        result_obj = item.result
        result_type = getattr(result_obj, "type", None)

        if result_type != "succeeded":
            chunks_rejected += 1
            error_detail = getattr(result_obj, "error", result_type)
            rejected_reasons.append(
                f"bars {target_chunk.context.bars.target}: batch error {error_detail}"
            )
            continue

        message = result_obj.message
        msg_usage = getattr(message, "usage", None)
        if msg_usage is not None:
            usage["input_tokens"] += getattr(msg_usage, "input_tokens", 0)
            usage["output_tokens"] += getattr(msg_usage, "output_tokens", 0)
            usage["cache_read_input_tokens"] += (
                getattr(msg_usage, "cache_read_input_tokens", 0) or 0
            )

        try:
            chunk_output = _parse_batch_message_content(message, target_chunk.context.bars.target)
        except L1ClientError as exc:
            chunks_rejected += 1
            rejected_reasons.append(f"bars {target_chunk.context.bars.target}: {exc}")
            continue

        validation_notes = _validation_notes_for_chunk(
            target_chunk, notes_by_id, chunk_output.decisions
        )
        violations = validate_decisions(
            chunk_output.decisions, notes=validation_notes, part_staves=part.staves
        )
        if violations:
            chunks_rejected += 1
            reasons = "; ".join(f"{v.rule}(note {v.note_id}): {v.message}" for v in violations)
            rejected_reasons.append(f"bars {target_chunk.context.bars.target}: {reasons}")
            continue

        chunks_ok += 1
        for decision in chunk_output.decisions:
            note = notes_by_id.get(decision.note_id)
            if note is None:
                continue
            if decision.action in _UNSUPPORTED_ACTIONS:
                skipped_decisions.append(
                    f"note {decision.note_id}: unsupported action {decision.action!r}"
                )
                continue
            note_bar, _ = tick_to_bar_beat(
                note.onset_tick or 0,
                time_signatures=time_signatures_raw,
                divisions=staged.divisions,
            )
            if decision.action == "keep":
                _apply_keep(note, decision, run_id=run_id, beat_anchors=beat_anchors)
            elif decision.action == "delete":
                _apply_delete(note, decision, run_id=run_id)
            elif decision.action == "split_tie":
                _apply_split_tie(
                    staged,
                    note,
                    decision,
                    note_bar=note_bar,
                    run_id=run_id,
                    time_signatures=time_signatures_raw,
                    divisions=staged.divisions,
                    beat_anchors=beat_anchors,
                )

    cost_usd = calculate_usage_cost_usd(usage, model, mode="batch")

    staged.meta.stages["refine"] = {
        "lanes_applied": ["L0", "L1"],
        "l1": {
            "mode": "batch",
            "model": model,
            "chunks_ok": chunks_ok,
            "chunks_rejected": chunks_rejected,
            "usage": usage,
            "cost_usd": cost_usd,
        },
    }

    return L1RunResult(
        staged_score=staged,
        chunks_ok=chunks_ok,
        chunks_rejected=chunks_rejected,
        rejected_reasons=rejected_reasons,
        skipped_decisions=skipped_decisions,
        usage=usage,
    )
