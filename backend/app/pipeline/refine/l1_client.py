"""L1: claude CLI経由でのStructured Outputs呼び出し(#104, 設計書§7.3)。

#39時点ではAnthropic SDK(`anthropic.Anthropic`)を直接呼び出していたが、これは
生の`AME_ANTHROPIC_API_KEY`の設定を必須にする。開発機やユーザー環境で既に
Claude Code CLI(`claude`)が認証済み(サブスクリプション/OAuth)であれば、別途
APIキーを発行・課金設定する必要が無い(#104: ユーザー指摘によるアーキテクチャ
変更)。`claude -p --output-format json --json-schema <schema> --effort <level>`
で、Anthropic Messages APIのStructured Outputsとほぼ同等の構造化出力・usage・
実測コスト(`total_cost_usd`)が取得できることを実機検証済み。

`--allowedTools "StructuredOutput" --restricted`でツール実行(Bash/Read/Edit等)
を一切許可せず、構造化出力を返す内部ツール`StructuredOutput`のみを許可する
(このチャンク注釈タスクはテキスト入力→JSON出力の純粋な変換であり、ファイル
アクセスやコマンド実行は不要かつ望ましくない)。

テストでは`subprocess.run`をモックし、実際に`claude`バイナリを呼び出さない
(CI(Windows GitHub Actions)には`claude` CLIがインストールされていない前提)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, Field, ValidationError

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

# 1チャンクあたりのCLIプロセスのタイムアウト。曲全体を跨ぐ処理ではなく
# 1回のstructured output呼び出しのみのため、通常は数十秒で完了する。
CLI_TIMEOUT_SEC: Final = 180
_CLAUDE_CLI_BIN: Final = "claude"
# `StructuredOutput`のみ許可する(#104の設計判断、モジュールdocstring参照)。
_ALLOWED_TOOLS: Final = "StructuredOutput"


class L1ChunkResponse(BaseModel):
    """L1の出力スキーマ(設計書§7.3の出力例)。`decisions`は

    `domain.invariants.Decision`をそのまま再利用し(検証層と同一のスキーマで
    受け取ることで変換ミスを防ぐ)、Structured Outputsの`output_format`として
    このモデルのJSON Schemaを`claude` CLIへ渡す。
    """

    # `tuple[int, int]`ではなく`list[int]`にする理由: JSON Schemaにすると
    # `tuple`は`prefixItems`を使うが、`claude` CLIの`--json-schema`はstrict
    # モードで`prefixItems`を未知キーワードとして拒否するため(#104で
    # 実機検証して判明)。このフィールド自体はどこからも消費されない
    # (チャンクの棄却理由等は`ChunkInput.context.bars.target`を使う)ため、
    # 型変更の影響範囲はスキーマ生成のみ。
    bar_range: list[int] = Field(min_length=2, max_length=2)
    decisions: list[Decision] = Field(default_factory=list)
    # #39では中身を消費しない(小節注釈はUI表示用、将来のDiffPanel/#41で使う想定)。
    bar_annotations: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class L1ChunkCallResult:
    """`call_l1_chunk`の戻り値。構造化出力に加え、コスト可視化(NFR-07)に

    必要なトークン使用量・実測コスト(`claude` CLIが返す`total_cost_usd`、
    モデル単価表を使った概算ではなく実際に課金された金額)も一緒に返す
    (呼び出し元がチャンクをまたいで累積する)。
    """

    output: L1ChunkResponse
    usage: dict[str, int]
    cost_usd: float


class L1ClientError(RuntimeError):
    """CLI呼び出し失敗(バイナリ未検出、非0終了、JSON解析失敗、

    構造化出力のスキーマ不一致、モデルによる拒否)。
    """


def is_claude_cli_available() -> bool:
    """`claude` CLIがPATH上に存在するか(#104: APIキーの代わりのゲート条件)。"""
    return shutil.which(_CLAUDE_CLI_BIN) is not None


_JSON_SCHEMA_CACHE: dict[str, Any] | None = None


def _l1_chunk_response_schema() -> dict[str, Any]:
    """`L1ChunkResponse`のJSON Schema。モジュールレベルで固定のため一度だけ計算する。"""
    global _JSON_SCHEMA_CACHE
    if _JSON_SCHEMA_CACHE is None:
        _JSON_SCHEMA_CACHE = L1ChunkResponse.model_json_schema()
    return _JSON_SCHEMA_CACHE


def call_l1_chunk(
    chunk: ChunkInput,
    *,
    system_prompt: str,
    song_context: str,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
) -> L1ChunkCallResult:
    """1チャンク分のStructured Outputsを`claude` CLI経由で呼び、`L1ChunkResponse`へ変換する。

    プロンプト構造(設計書§7.3の意図を踏襲): 役割定義+記譜ルール集+Dorico制約は
    `--append-system-prompt`で付与し、楽曲全体コンテキスト+チャンク入力JSONを
    メインプロンプトとして渡す。CLIは`cache_control`ブレークポイントを明示的に
    指定できないため、キャッシュ効率はAnthropic側の自動キャッシュ挙動に委ねる
    (実機確認では同一の付加システムプロンプト+前方一致するプロンプトに対して
    `cache_read_input_tokens`が実際にヒットすることを確認済みだが、生API呼び出し
    ほど明示的な制御はできないトレードオフとして受け入れる、#104)。
    """
    if not is_claude_cli_available():
        raise L1ClientError(f"{_CLAUDE_CLI_BIN!r} CLI not found on PATH")

    prompt = f"{song_context}\n\n{build_chunk_message(chunk)}"
    cmd = [
        _CLAUDE_CLI_BIN,
        "-p",
        prompt,
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(_l1_chunk_response_schema()),
        "--model",
        model,
        "--effort",
        effort,
        "--allowedTools",
        _ALLOWED_TOOLS,
        "--restricted",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise L1ClientError(
            f"claude CLI invocation failed for chunk {chunk.context.bars.target}: {exc}"
        ) from exc

    if proc.returncode != 0:
        raise L1ClientError(
            f"claude CLI exited with status {proc.returncode} for chunk "
            f"{chunk.context.bars.target}: {proc.stderr.strip()[:500]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise L1ClientError(
            f"claude CLI returned non-JSON output for chunk {chunk.context.bars.target}: {exc}"
        ) from exc

    if payload.get("is_error"):
        raise L1ClientError(
            f"claude CLI reported an error for chunk {chunk.context.bars.target}: "
            f"{payload.get('result')!r}"
        )

    structured = payload.get("structured_output")
    if structured is None:
        raise L1ClientError(
            f"claude CLI produced no structured_output for chunk {chunk.context.bars.target} "
            f"(result={payload.get('result')!r})"
        )

    try:
        output = L1ChunkResponse.model_validate(structured)
    except ValidationError as exc:
        raise L1ClientError(
            f"structured output failed schema validation for chunk "
            f"{chunk.context.bars.target}: {exc}"
        ) from exc

    usage_raw = payload.get("usage") or {}
    usage = {
        "input_tokens": usage_raw.get("input_tokens", 0) or 0,
        "output_tokens": usage_raw.get("output_tokens", 0) or 0,
        "cache_read_input_tokens": usage_raw.get("cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": usage_raw.get("cache_creation_input_tokens", 0) or 0,
    }
    cost_usd = float(payload.get("total_cost_usd") or 0.0)

    return L1ChunkCallResult(output=output, usage=usage, cost_usd=cost_usd)
