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

`is_claude_cli_available()`はPATH上の存在確認に加え`claude auth status --json`
で認証済みかも確認する(#104 Gate2レビュー指摘)。メインプロンプト(楽曲コン
テキスト+チャンク入力JSON)はコマンドライン引数ではなく標準入力経由で渡す
(#104 Gate2レビュー指摘: Windowsのコマンドライン長上限を超えるリスクを回避)。
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
# `claude auth status`確認用の短いタイムアウト(#104 Gate2レビュー指摘)。
_AUTH_CHECK_TIMEOUT_SEC: Final = 10
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
    """`claude` CLIが使える状態か(#104: APIキーの代わりのゲート条件)。

    PATH上の存在確認だけでなく`claude auth status --json`で認証済み
    (`loggedIn: true`)であることも確認する(#104 Gate2レビュー指摘: PATH上に
    バイナリがあるだけで未認証の場合、このゲートを素通りしてチャンクごとの
    CLI呼び出しが失敗し、503(NFR-12が意図する明確な「AI未設定」メッセージ)
    ではなく502(L1ClientError)になってしまう)。
    """
    if shutil.which(_CLAUDE_CLI_BIN) is None:
        return False
    try:
        proc = subprocess.run(
            [_CLAUDE_CLI_BIN, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_AUTH_CHECK_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    # #104 Gate2レビュー指摘(2巡目): `claude auth status --json`が有効なJSON
    # だがオブジェクトでない(`null`等)場合、`.get`が`AttributeError`を送出し
    # このゲート関数自体が例外で落ちる。dict以外は未認証扱いにする。
    if not isinstance(data, dict):
        return False
    return bool(data.get("loggedIn"))


# `L1ChunkResponse`のJSON Schema。モジュールimport時に一度だけ計算する
# (#104 Gate2レビュー指摘: 遅延初期化だと`run_l1_batch`の`ThreadPoolExecutor`
# から複数スレッドが同時に到達しうる。`model_json_schema()`自体は冪等だが、
# 排他制御を要らなくするため最初から計算しておく)。
_L1_CHUNK_RESPONSE_SCHEMA: Final[dict[str, Any]] = L1ChunkResponse.model_json_schema()


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

    メインプロンプト(`song_context`+チャンク入力JSON)はコマンドライン引数
    ではなく標準入力経由で渡す(#104 Gate2レビュー指摘: CI/本番はWindowsで
    あり、コマンドライン長には約32,767文字の上限があるため、ノート数が多い
    チャンクでは引数長がこれを超えうる。`--append-system-prompt`のシステム
    プロンプトと`--json-schema`は固定サイズ(それぞれ約1,000字/約3,100字)で
    上限に達する現実的なリスクが無いため引数のまま残す)。

    `is_claude_cli_available()`はここでは呼ばない(#104 Gate2レビュー指摘、
    2巡目): 呼び出し元(`api/refine.py`)がrun開始前に一度だけゲートしており、
    チャンクごとに`claude auth status`サブプロセス(最大10秒)を追加で起動する
    のは無駄なオーバーヘッドになる。CLI自体が見つからない/認証切れの場合は
    後続の`subprocess.run`が`OSError`または非0終了として自然に失敗し、
    `L1ClientError`へ変換される。
    """
    prompt = f"{song_context}\n\n{build_chunk_message(chunk)}"
    cmd = [
        _CLAUDE_CLI_BIN,
        "-p",
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(_L1_CHUNK_RESPONSE_SCHEMA),
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
            input=prompt,
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

    # #104 Gate2レビュー指摘(2巡目、HIGH): `stdout`が有効なJSONだがオブジェクト
    # でない(`null`/配列/数値等)場合、以降の`payload.get(...)`が
    # `AttributeError`を送出する。これは`L1ClientError`ではないため
    # `l1_batch.py`の`_call_chunk`(`L1ClientError`しか捕捉しない)を素通りし、
    # `executor.map`のイテレーションが他チャンクの結果ごと失われる
    # (`api/refine.py`の`except L1ClientError`にも掛からず500になる)。
    if not isinstance(payload, dict):
        raise L1ClientError(
            f"claude CLI returned unexpected JSON (not an object) for chunk "
            f"{chunk.context.bars.target}: {payload!r}"
        )

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

    # #104 Gate2レビュー指摘: CLIが想定外の型(`usage`がdict以外、
    # `total_cost_usd`が数値に変換できない文字列等)を返すと、ここで
    # `TypeError`/`ValueError`/`AttributeError`が未捕捉のまま伝播し、
    # `run_l1_batch`の`executor.map`イテレーション時に他チャンクの結果ごと
    # 失われる(`_call_chunk`は`L1ClientError`しか値へ変換していないため)。
    # 構造化出力(decisions)の正しさには影響しない付随情報であるため、
    # 例外は`L1ClientError`へ変換してこのチャンクの棄却として隔離する。
    try:
        usage_raw = payload.get("usage") or {}
        if not isinstance(usage_raw, dict):
            raise TypeError(f"usage field is not an object: {usage_raw!r}")
        usage = {
            "input_tokens": int(usage_raw.get("input_tokens") or 0),
            "output_tokens": int(usage_raw.get("output_tokens") or 0),
            "cache_read_input_tokens": int(usage_raw.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(usage_raw.get("cache_creation_input_tokens") or 0),
        }
        cost_usd = float(payload.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError) as exc:
        raise L1ClientError(
            f"claude CLI returned malformed usage/cost fields for chunk "
            f"{chunk.context.bars.target}: {exc}"
        ) from exc

    return L1ChunkCallResult(output=output, usage=usage, cost_usd=cost_usd)
