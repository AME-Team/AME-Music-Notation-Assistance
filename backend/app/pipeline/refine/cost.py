"""L1/L2 AI 整音のトークン使用量・コスト計算モジュール(#40, 設計書§7.5/NFR-07/R-9)。

- NFR-07: トークン使用量とコストの実測・可視化。
- R-9: Batch API (50%割引) によるコスト抑制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


@dataclass(frozen=True)
class ModelPricing:
    """1,000,000 トークンあたりの USD 単価。"""

    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_creation_per_million: float


# Anthropic Messages Batches API は通常料金の 50% 割引 (R-9)
BATCH_DISCOUNT_FACTOR: Final[float] = 0.5

# 設計書§7.5 の試算モデルに準拠:
# - claude-opus-5: 逐次 約$3.5 / Batch 約$1.8
#   (Input $5.00 / Output $25.00 / CacheRead $0.50 / CacheWrite $6.25 per MTok)
# - claude-sonnet-5: Batch 約$1.1
#   (Input $3.00 / Output $15.00 / CacheRead $0.30 / CacheWrite $3.75 per MTok)
PRICING_TABLE: Final[dict[str, ModelPricing]] = {
    "claude-opus-5": ModelPricing(
        input_per_million=5.0,
        output_per_million=25.0,
        cache_read_per_million=0.5,
        cache_creation_per_million=6.25,
    ),
    "claude-sonnet-5": ModelPricing(
        input_per_million=3.0,
        output_per_million=15.0,
        cache_read_per_million=0.3,
        cache_creation_per_million=3.75,
    ),
}

DEFAULT_PRICING: Final[ModelPricing] = PRICING_TABLE["claude-opus-5"]


@dataclass(frozen=True)
class RefineCostEstimate:
    """L1 事前見積もりの結果構造体。"""

    num_chunks: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cache_read_tokens: int
    estimated_cost_usd: float


def calculate_usage_cost_usd(
    usage: dict[str, int],
    model: str,
    mode: Literal["sync", "batch"],
) -> float:
    """実測トークン使用量から USD コストを算出する (NFR-07)。

    Anthropic API レスポンスの usage フィールド:
    - input_tokens: キャッシュを含まない非キャッシュ入力トークン数
    - output_tokens: 生成出力トークン数
    - cache_read_input_tokens: キャッシュから読み込まれた入力トークン数 (90%引)
    - cache_creation_input_tokens: キャッシュ書き込みトークン数 (通常単価の1.25倍)
    """
    pricing = PRICING_TABLE.get(model, DEFAULT_PRICING)
    discount = BATCH_DISCOUNT_FACTOR if mode == "batch" else 1.0

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

    cost = (
        (input_tokens * pricing.input_per_million / 1_000_000.0)
        + (cache_creation_tokens * pricing.cache_creation_per_million / 1_000_000.0)
        + (cache_read_tokens * pricing.cache_read_per_million / 1_000_000.0)
        + (output_tokens * pricing.output_per_million / 1_000_000.0)
    ) * discount

    return round(cost, 4)


def estimate_refine_cost(
    num_chunks: int,
    model: str,
    mode: Literal["sync", "batch"],
) -> RefineCostEstimate:
    """チャンク数から事前見積もりを算出する (設計書§7.5)。

    §7.5 の試算モデル:
    - システムプロンプト: 1,500 トークン (キャッシュ)
    - 曲コンテキスト: 500 トークン (キャッシュ)
    - チャンク入力: 2,500 トークン / チャンク
    - 出力: 1,500 トークン / チャンク
    - キャッシュ効率:
      - 初回: プロンプト 2,000 トークンがキャッシュ作成 (cache_creation)
      - 2 チャンク目以降: プロンプト 2,000 トークンがキャッシュヒット (cache_read)
    """
    if num_chunks <= 0:
        return RefineCostEstimate(
            num_chunks=0,
            estimated_input_tokens=0,
            estimated_output_tokens=0,
            estimated_cache_read_tokens=0,
            estimated_cost_usd=0.0,
        )

    prompt_tokens = 2000
    chunk_input_tokens = 2500
    chunk_output_tokens = 1500

    total_chunk_input = chunk_input_tokens * num_chunks
    total_input = prompt_tokens + total_chunk_input
    total_cache_read = prompt_tokens * max(0, num_chunks - 1)
    cache_creation_tokens = prompt_tokens
    total_output = chunk_output_tokens * num_chunks

    usage_for_calc = {
        "input_tokens": total_chunk_input,
        "cache_creation_input_tokens": cache_creation_tokens,
        "cache_read_input_tokens": total_cache_read,
        "output_tokens": total_output,
    }

    cost_usd = calculate_usage_cost_usd(usage_for_calc, model, mode)

    return RefineCostEstimate(
        num_chunks=num_chunks,
        estimated_input_tokens=total_input,
        estimated_output_tokens=total_output,
        estimated_cache_read_tokens=total_cache_read,
        estimated_cost_usd=cost_usd,
    )
