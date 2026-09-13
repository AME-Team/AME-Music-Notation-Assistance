"""#40: コスト計算モジュール(`app.pipeline.refine.cost`)のテスト。"""

from __future__ import annotations

from app.pipeline.refine.cost import (
    calculate_usage_cost_usd,
    estimate_refine_cost,
)


def test_calculate_usage_cost_sync_opus():
    # 80k input, 50k output, 20k cache_read, 2k cache_creation
    usage = {
        "input_tokens": 80_000,
        "output_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 2_000,
    }
    # input: 80k @ $5/M = $0.40
    # cache_creation: 2k @ $6.25/M = $0.0125
    # cache_read: 20k @ $0.5/M = $0.01
    # output: 50k @ $25/M = $1.25
    # total = $1.6725
    cost = calculate_usage_cost_usd(usage, model="claude-opus-5", mode="sync")
    assert cost == 1.6725


def test_calculate_usage_cost_batch_discount():
    usage = {
        "input_tokens": 80_000,
        "output_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 2_000,
    }
    # Batch is 50% discount -> $1.6725 * 0.5 = $0.83625 -> round 0.8363
    cost = calculate_usage_cost_usd(usage, model="claude-opus-5", mode="batch")
    assert cost == 0.8363


def test_calculate_usage_cost_sonnet():
    usage = {
        "input_tokens": 80_000,
        "output_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 2_000,
    }
    # input: 80k @ $3/M = $0.24
    # cache_creation: 2k @ $3.75/M = $0.0075
    # cache_read: 20k @ $0.3/M = $0.006
    # output: 50k @ $15/M = $0.75
    # total = $1.0035 -> round(1.0035, 4)
    cost = calculate_usage_cost_usd(usage, model="claude-sonnet-5", mode="sync")
    assert cost == 1.0035


def test_estimate_refine_cost_zero_chunks():
    est = estimate_refine_cost(0, model="claude-opus-5", mode="batch")
    assert est.num_chunks == 0
    assert est.estimated_cost_usd == 0.0


def test_estimate_refine_cost_70_chunks_aligns_with_design_spec():
    """設計書§7.5の試算値と整合しているかを検証。

    §7.5: 70チャンクで
    Opus 5 / 逐次: 約 $3.5 (試算値 $3.58)
    Opus 5 / Batch API: 約 $1.8 (試算値 $1.79)
    Sonnet 5 / Batch API: 約 $1.1 (試算値 $1.07)
    """
    est_sync = estimate_refine_cost(70, model="claude-opus-5", mode="sync")
    est_batch = estimate_refine_cost(70, model="claude-opus-5", mode="batch")
    est_sonnet_batch = estimate_refine_cost(70, model="claude-sonnet-5", mode="batch")

    assert est_sync.num_chunks == 70
    assert est_batch.num_chunks == 70
    # プロンプト(2,000) + チャンク入力(2,500 * 70) = 177,000 トークン
    assert est_sync.estimated_input_tokens == 177_000
    assert est_sync.estimated_output_tokens == 105_000
    assert est_sync.estimated_cache_read_tokens == 138_000
    assert est_sync.estimated_cost_usd == 3.5815
    assert est_batch.estimated_cost_usd == 1.7908
    assert est_sonnet_batch.estimated_cost_usd == 1.0744
