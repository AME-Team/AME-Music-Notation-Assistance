"""L1/L2 AI 整音の事前見積もりモジュール(#40/#104, 設計書§7.5/NFR-07)。

- NFR-07: トークン使用量とコストの実測・可視化。
- #104: L1のLLM呼び出しを`claude` CLI経由に変更したことに伴い、実行後の実測
  コストは各チャンクの`claude` CLIが返す`total_cost_usd`の合計をそのまま使う
  (`pipeline/refine/l1_runner.py`/`l1_batch.py`の`L1RunResult.cost_usd`)。
  このモジュールが提供する単価表ベースの計算は、実行前の事前見積もり
  (`GET /refine/estimate`)専用となる。`mode="batch"`はAnthropic Batches API
  (50%割引)ではなく並列claude CLI呼び出しに再定義されたため、割引係数は廃止した。
  ただし`mode`は事前見積もりの**キャッシュ再利用前提**には引き続き影響する
  (`estimate_refine_cost`のdocstring参照、#104 Gate2レビュー指摘)。
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


# 設計書§7.5 の試算モデルに準拠(逐次実行時の単価。#104でbatch=並列実行に
# 再定義されたため、単価自体はsync/batch共通):
# - claude-opus-5: 約$3.5/曲
#   (Input $5.00 / Output $25.00 / CacheRead $0.50 / CacheWrite $6.25 per MTok)
# - claude-sonnet-5: 約$2.2/曲
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


def calculate_usage_cost_usd(usage: dict[str, int], model: str) -> float:
    """トークン使用量から USD コストを算出する (NFR-07、事前見積もり専用)。

    実行後の実測コストは`claude` CLIが返す`total_cost_usd`をそのまま使うため
    (#104、`L1RunResult.cost_usd`)、この関数は`estimate_refine_cost`(実行前の
    事前見積もり、まだトークン数が概算値でしかない)からのみ呼ばれる。

    見積もりの元になるトークン内訳:
    - input_tokens: キャッシュを含まない非キャッシュ入力トークン数
    - output_tokens: 生成出力トークン数
    - cache_read_input_tokens: キャッシュから読み込まれた入力トークン数 (90%引)
    - cache_creation_input_tokens: キャッシュ書き込みトークン数 (通常単価の1.25倍)
    """
    pricing = PRICING_TABLE.get(model, DEFAULT_PRICING)

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

    cost = (
        (input_tokens * pricing.input_per_million / 1_000_000.0)
        + (cache_creation_tokens * pricing.cache_creation_per_million / 1_000_000.0)
        + (cache_read_tokens * pricing.cache_read_per_million / 1_000_000.0)
        + (output_tokens * pricing.output_per_million / 1_000_000.0)
    )

    return round(cost, 4)


def estimate_refine_cost(
    num_chunks: int, model: str, mode: Literal["sync", "batch"] = "sync"
) -> RefineCostEstimate:
    """チャンク数から事前見積もりを算出する (設計書§7.5)。

    #104: `mode="batch"`は並列claude CLI呼び出しに再定義され、Anthropic Batches
    APIの50%割引に相当する仕組みは無いため、割引は行わない。ただし`mode`は
    キャッシュ再利用の前提には引き続き影響する(#104 Gate2レビュー指摘):
    `sync`(逐次実行)では2チャンク目以降が前のチャンクのプロンプトキャッシュを
    `cache_read`(割安)として再利用できるが、`batch`(並列実行)では複数チャンクが
    同時多発的にリクエストされるため、キャッシュがまだ書き込み中の状態で
    互いに追い越し合い、ほぼ再利用が効かない。安全側に倒し、`batch`では
    全チャンクが`cache_creation`(通常単価の1.25倍、`cache_read`より高価)を
    個別に負担する前提で見積もる。

    §7.5 の試算モデル:
    - システムプロンプト: 1,500 トークン (キャッシュ)
    - 曲コンテキスト: 500 トークン (キャッシュ)
    - チャンク入力: 2,500 トークン / チャンク
    - 出力: 1,500 トークン / チャンク
    - キャッシュ効率(sync):
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
    total_output = chunk_output_tokens * num_chunks

    if mode == "batch":
        # 全チャンクがキャッシュ再利用なしでcache_creationを個別に負担する。
        total_cache_read = 0
        cache_creation_tokens = prompt_tokens * num_chunks
    else:
        total_cache_read = prompt_tokens * max(0, num_chunks - 1)
        cache_creation_tokens = prompt_tokens

    usage_for_calc = {
        "input_tokens": total_chunk_input,
        "cache_creation_input_tokens": cache_creation_tokens,
        "cache_read_input_tokens": total_cache_read,
        "output_tokens": total_output,
    }

    cost_usd = calculate_usage_cost_usd(usage_for_calc, model)

    return RefineCostEstimate(
        num_chunks=num_chunks,
        estimated_input_tokens=total_input,
        estimated_output_tokens=total_output,
        estimated_cache_read_tokens=total_cache_read,
        estimated_cost_usd=cost_usd,
    )
