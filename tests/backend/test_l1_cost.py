"""#40: コスト計算モジュール(`app.pipeline.refine.cost`)のテスト。"""

from __future__ import annotations

from app.pipeline.refine.cost import (
    calculate_usage_cost_usd,
    estimate_refine_cost,
)


def test_calculate_usage_cost_sync_opus():
    # 100k input, 50k output, 20k cache_read
    usage = {
        "input_tokens": 100_000,
        "output_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
    }
    # billable input: 80k @ $5/M = $0.40
    # output: 50k @ $25/M = $1.25
    # cache read: 20k @ $0.5/M = $0.01
    # total = $1.66
    cost = calculate_usage_cost_usd(usage, model="claude-opus-5", mode="sync")
    assert cost == 1.66


def test_calculate_usage_cost_batch_discount():
    usage = {
        "input_tokens": 100_000,
        "output_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
    }
    # Batch is 50% discount -> $1.66 * 0.5 = $0.83
    cost = calculate_usage_cost_usd(usage, model="claude-opus-5", mode="batch")
    assert cost == 0.83


def test_calculate_usage_cost_sonnet():
    usage = {
        "input_tokens": 100_000,
        "output_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
    }
    # billable input: 80k @ $3/M = $0.24
    # output: 50k @ $15/M = $0.75
    # cache read: 20k @ $0.3/M = $0.006
    # total = $0.996 -> round(0.996, 4)
    cost = calculate_usage_cost_usd(usage, model="claude-sonnet-5", mode="sync")
    assert cost == 0.996


def test_estimate_refine_cost_zero_chunks():
    est = estimate_refine_cost(0, model="claude-opus-5", mode="batch")
    assert est.num_chunks == 0
    assert est.estimated_cost_usd == 0.0


def test_estimate_refine_cost_70_chunks_aligns_with_design_spec():
    """設計書§7.5の試算値と整合しているかを検証。

    §7.5: 70チャンクで
    Opus 5 / 逐次: 約 $3.5
    Opus 5 / Batch API: 約 $1.8
    Sonnet 5 / Batch API: 約 $1.1
    """
    est_sync = estimate_refine_cost(70, model="claude-opus-5", mode="sync")
    est_batch = estimate_refine_cost(70, model="claude-opus-5", mode="batch")
    est_sonnet_batch = estimate_refine_cost(70, model="claude-sonnet-5", mode="batch")

    assert est_sync.num_chunks == 70
    assert est_batch.num_chunks == 70
    assert est_sync.estimated_cost_usd == 3.50
    assert est_batch.estimated_cost_usd == 1.75
    assert est_sonnet_batch.estimated_cost_usd == 1.05
