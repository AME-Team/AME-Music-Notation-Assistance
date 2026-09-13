"""L1: Anthropic Structured Outputsの呼び出し(#39, 設計書§7.3)。

`client`を引数として受け取ることで、テストでは`unittest.mock.Mock`を注入して
実ネットワーク呼び出しを一切発生させずに配線(リクエスト形状・レスポンス変換・
エラー処理)を検証できるようにする(このリポジトリに実際のAPIキーは無い)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import anthropic
from pydantic import BaseModel, Field

from app.domain.invariants import Decision
from app.pipeline.refine.l1_prompt import build_chunk_message

if TYPE_CHECKING:
    from app.pipeline.refine.l1_chunker import ChunkInput

# 設計書§7.3 API設定表。型注記を省略した`Final`(明示的な`Literal[...]`は
# 付けない)にすることで、mypyが代入値からそのまま狭いLiteral型
# (`Literal["high"]`等)を推論し、`api/refine.py`の
# `RefineRequest.effort: Literal["high", "medium"]`のデフォルト値として
# 型チェックを通す(単なる`str: str = "high"`の書き方だと`str`型に
# 広がってしまい不整合になる、mypy指摘、#39 Gate2レビュー指摘で
# コメントの記述を実装に合わせて修正)。
DEFAULT_MODEL: Final = "claude-opus-5"
DEFAULT_EFFORT: Final = "high"
MAX_TOKENS = 16000


class L1ChunkResponse(BaseModel):
    """L1の出力スキーマ(設計書§7.3の出力例)。`decisions`は

    `domain.invariants.Decision`をそのまま再利用し(検証層と同一のスキーマで
    受け取ることで変換ミスを防ぐ)、Structured Outputsの`output_format`として
    このモデルをAnthropic SDKへ渡す。
    """

    bar_range: tuple[int, int]
    decisions: list[Decision] = Field(default_factory=list)
    # #39では中身を消費しない(小節注釈はUI表示用、将来のDiffPanel/#41で使う想定)。
    bar_annotations: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class L1ChunkCallResult:
    """`call_l1_chunk`の戻り値。構造化出力に加え、コスト可視化(NFR-07)に

    必要なトークン使用量も一緒に返す(呼び出し元がチャンクをまたいで累積する)。
    """

    output: L1ChunkResponse
    usage: dict[str, int]


class L1ClientError(RuntimeError):
    """API呼び出し失敗(refusal、構造化出力のパース失敗、またはSDKレベルの

    エラー)。設計書§7.3のリトライ表: `stop_reason == "refusal"`は検証失敗
    扱いにする(429/5xxはAnthropic SDK自身が既定でリトライ済みのため、
    ここまで来た時点でリトライ後もなお失敗したことを意味する)。

    `anthropic.AnthropicError`(`APIConnectionError`/`RateLimitError`/
    `APIStatusError`/`APIResponseValidationError`等、SDKが送出しうる例外の
    共通基底クラス)もここに包んで送出する(#39 Gate2レビュー指摘: 以前は
    refusal/パース失敗しか変換しておらず、SDK例外がそのまま伝播すると
    呼び出し元(`l1_runner.py`)の`except L1ClientError`で捕捉できず、
    チャンク単位の棄却ではなくリクエスト全体が500になっていた)。
    """


def call_l1_chunk(
    chunk: ChunkInput,
    *,
    client: anthropic.Anthropic,
    system_prompt: str,
    song_context: str,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> L1ChunkCallResult:
    """1チャンク分のStructured Outputsを呼び、`L1ChunkResponse`へ変換する。

    プロンプト構造(設計書§7.3、キャッシュ効率を意識した順序):
    system(役割定義+記譜ルール集+Dorico制約、曲をまたいでキャッシュ) →
    楽曲全体コンテキスト(曲単位でキャッシュ) → チャンク入力JSON(毎回変わる)。
    `cache_control: {"type": "ephemeral"}`を該当ブロックに付与する。
    """
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[
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
            output_config={"effort": effort},
            output_format=L1ChunkResponse,
            thinking={"type": "adaptive"},
        )
    except anthropic.AnthropicError as exc:
        raise L1ClientError(
            f"Anthropic API call failed for chunk {chunk.context.bars.target}: {exc}"
        ) from exc

    if response.stop_reason == "refusal":
        raise L1ClientError(f"model refused chunk {chunk.context.bars.target}")
    if response.parsed_output is None:
        raise L1ClientError(
            f"failed to parse structured output for chunk {chunk.context.bars.target}"
        )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
    }
    return L1ChunkCallResult(output=response.parsed_output, usage=usage)
