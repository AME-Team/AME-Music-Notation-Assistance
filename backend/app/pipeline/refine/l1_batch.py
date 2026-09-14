"""L1: 複数チャンクの並列claude CLI呼び出しによる一括実行(#104, 設計書§7.3/§7.5)。

#40時点ではAnthropic Messages Batches API(50%割引)を使っていたが、`claude` CLI
呼び出し方式(#104)にはBatches API相当の割引機構が無い。そのため`mode="batch"`は
「複数チャンクを`ThreadPoolExecutor`で並列実行し、体感速度を上げるモード」として
再定義する(#104: ユーザーと合意した設計判断。コスト割引を伴わない点が#40との
違い)。

LLM呼び出し自体は並列化するが、検証(V-1〜V-9)とステージングScoreIRへの適用
(`verify_and_apply_chunk_decisions`)は`staged`/`notes_by_id`を共有ミューテートする
ため、並列化せず元のチャンク順で逐次実行する(`run_l1_sequential`と同じ適用順序を
保ち、結果を決定論的にするため)。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

from app.pipeline.refine.l1_chunker import build_chunks
from app.pipeline.refine.l1_client import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    L1ChunkCallResult,
    L1ClientError,
    call_l1_chunk,
)
from app.pipeline.refine.l1_prompt import build_song_context_message, build_system_prompt
from app.pipeline.refine.l1_runner import L1RunResult, verify_and_apply_chunk_decisions

if TYPE_CHECKING:
    from app.domain.score import ScoreIR
    from app.pipeline.refine.l1_chunker import ChunkInput

# 同時実行数。`claude` CLIプロセスを無制限に同時起動するとローカルマシンや
# レート制限を圧迫するため上限を設ける(#104設計判断、既定値は経験則)。
MAX_CONCURRENCY: Final = 4


def _call_chunk(
    chunk: ChunkInput, *, system_prompt: str, song_context: str, model: str, effort: str
) -> L1ChunkCallResult | L1ClientError:
    """`ThreadPoolExecutor.map`で使うラッパ。例外を戻り値として返すことで、

    1チャンクの失敗が他チャンクの並列実行を止めない(`executor.map`は
    イテレート時に例外を再送出するため、そのままだと1件の失敗で残りの
    結果を読み出せなくなる)。
    """
    try:
        return call_l1_chunk(
            chunk,
            system_prompt=system_prompt,
            song_context=song_context,
            model=model,
            effort=effort,
        )
    except L1ClientError as exc:
        return exc


def run_l1_batch(
    score: ScoreIR,
    part_id: str,
    *,
    run_id: str,
    beat_anchors: list[tuple[float, float]],
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_concurrency: int = MAX_CONCURRENCY,
) -> L1RunResult:
    """1パートを対象に、全チャンクを並列claude CLI呼び出しで実行する。

    `score`自体は変更せず複製(deep copy)に対して操作する。全チャンクのLLM
    呼び出し完了を待ってから、元のチャンク順で検証・適用する。
    """
    if score.find_part(part_id) is None:
        raise ValueError(f"part {part_id!r} not found in score")

    staged = score.model_copy(deep=True)
    part = staged.find_part(part_id)
    assert part is not None
    notes_by_id = {n.id: n for n in part.notes}

    chunks = build_chunks(score, part_id)
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    if not chunks:
        return L1RunResult(staged_score=staged, chunks_ok=0, chunks_rejected=0, usage=usage)

    system_prompt = build_system_prompt()
    song_context = build_song_context_message(score, part_id)
    time_signatures_raw = [ts.model_dump(mode="json") for ts in staged.time_signatures]

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        call_results = list(
            executor.map(
                lambda chunk: _call_chunk(
                    chunk,
                    system_prompt=system_prompt,
                    song_context=song_context,
                    model=model,
                    effort=effort,
                ),
                chunks,
            )
        )

    chunks_ok = 0
    chunks_rejected = 0
    rejected_reasons: list[str] = []
    skipped_decisions: list[str] = []
    cost_usd = 0.0

    for chunk, call_result in zip(chunks, call_results, strict=True):
        if isinstance(call_result, L1ClientError):
            chunks_rejected += 1
            rejected_reasons.append(f"bars {chunk.context.bars.target}: {call_result}")
            continue

        for key in call_result.usage:
            usage[key] = usage.get(key, 0) + call_result.usage[key]
        cost_usd += call_result.cost_usd

        ok, rejection_reason = verify_and_apply_chunk_decisions(
            chunk=chunk,
            decisions=call_result.output.decisions,
            staged=staged,
            part_staves=part.staves,
            notes_by_id=notes_by_id,
            run_id=run_id,
            beat_anchors=beat_anchors,
            time_signatures_raw=time_signatures_raw,
            skipped_decisions=skipped_decisions,
        )
        if ok:
            chunks_ok += 1
        else:
            chunks_rejected += 1
            if rejection_reason:
                rejected_reasons.append(rejection_reason)

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
        cost_usd=cost_usd,
    )
