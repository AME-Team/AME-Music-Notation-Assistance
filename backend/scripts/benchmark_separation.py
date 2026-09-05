"""Stage 1分離のCPU/DirectML実測ベンチマーク(#17, NFR-01)。

CPU Execution Providerでの分離を実測し、`onnxruntime` が `DmlExecutionProvider` を
報告している場合はDirectMLでも実行して比較する。DirectMLはWindows専用であり、この
開発サンドボックス(Linux)の `onnxruntime`(CPU版)には同梱されないため、Linux上では
自動的にCPUのみの実行になる。DirectML実測とNFR-01の最終確定にはユーザーのWindows
実機(Radeon 780M)での実行が必要(README「既知の制約」参照)。

使い方:
    uv run --project backend python backend/scripts/benchmark_separation.py <audio_path> \
        [--preset fast|standard|high_quality] [--runs 3] [--out report.md]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.separate import (  # noqa: E402
    ExecutionProviderChoice,
    resolve_model,
    run_separation,
)

DEFAULT_RUNS = 3


def _detect_directml() -> bool:
    """`onnxruntime` が `DmlExecutionProvider` を報告しているか確認する。

    Linux版の `onnxruntime`(CPU版)はDirectMLプロバイダを同梱しないため、この
    開発サンドボックスでは常に False になる。Windows実機で `onnxruntime-directml`
    (もしくはDirectML対応ビルド)を導入している場合のみ True になる。

    注意: これは `onnxruntime` のビルドがDirectMLプロバイダを同梱しているかどうかの
    確認に過ぎない。実際にセッション作成時にDMLデバイス初期化が成功し、CPUへ黙って
    フォールバックしていないかまではここでは検証しない(#17-M1レビュー指摘: GPU/
    ドライバ起因でDMLセッション生成に失敗すると `run_separation` 側の
    `_EP_TO_ONNX_PROVIDERS["directml"]` が `["DmlExecutionProvider",
    "CPUExecutionProvider"]` の並びによりCPUへ黙って落ちるため、"directml" ラベルの
    計測値が実はCPU計測である可能性が残る)。この不確実性は `_write_report` の
    注記で明示する。
    """
    import onnxruntime

    return "DmlExecutionProvider" in onnxruntime.get_available_providers()


def _onnxruntime_version() -> str:
    import onnxruntime

    return str(onnxruntime.__version__)


def _run_once(
    audio_path: Path, preset: str, execution_provider: ExecutionProviderChoice, output_dir: Path
) -> float:
    start = time.perf_counter()
    run_separation(audio_path, output_dir, preset=preset, execution_provider=execution_provider)
    return time.perf_counter() - start


def _measure(
    audio_path: Path,
    preset: str,
    execution_provider: ExecutionProviderChoice,
    runs: int,
) -> float:
    """ウォームアップ1回(計測に含めない)+ `runs` 回計測し、中央値を返す。

    #17-M1レビュー指摘: 単発計測だと、初回実行に含まれるモデルダウンロード
    (htdemucs_6sで約258MB)・初回モデルロードのコストが計測値をまるごと歪める
    (特にCPUを先に計測しDirectMLを後で計測する既存の実行順だと、DirectML側だけ
    ダウンロード済みキャッシュの恩恵を受け、両者が対等な比較にならない)。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _run_once(audio_path, preset, execution_provider, Path(tmp) / "warmup")

    samples: list[float] = []
    for i in range(runs):
        with tempfile.TemporaryDirectory() as tmp:
            samples.append(_run_once(audio_path, preset, execution_provider, Path(tmp) / f"run{i}"))
    return statistics.median(samples)


def run_benchmark(audio_path: Path, preset: str, runs: int = DEFAULT_RUNS) -> dict[str, float]:
    model = resolve_model(preset)
    results: dict[str, float] = {}

    results["cpu"] = _measure(audio_path, preset, "cpu", runs)

    if _detect_directml():
        results["directml"] = _measure(audio_path, preset, "directml", runs)
    else:
        print(
            "[benchmark] DmlExecutionProvider not available in this onnxruntime "
            "installation (expected on Linux). Skipping DirectML measurement. "
            "Run this script on Windows with a DirectML-enabled onnxruntime to "
            "measure DirectML performance (see README known limitations)."
        )

    print(f"[benchmark] model={model} preset={preset} runs={runs} (median of {runs}, +1 warmup)")
    for provider, seconds in results.items():
        print(f"[benchmark]   {provider}: {seconds:.2f}s")
    return results


def _write_report(
    out_path: Path, audio_path: Path, preset: str, runs: int, results: dict[str, float]
) -> None:
    model = resolve_model(preset)
    lines = [
        "# Stage 1 分離ベンチマーク結果",
        "",
        f"- 入力: `{audio_path}`",
        f"- プリセット: `{preset}` (モデル: `{model}`)",
        f"- 計測方法: ウォームアップ1回(計測対象外)+ {runs}回計測の中央値",
        f"- onnxruntime バージョン: `{_onnxruntime_version()}`",
        "",
        "| Execution Provider | 所要時間 (秒、中央値) |",
        "| :--- | ---: |",
    ]
    for provider, seconds in results.items():
        lines.append(f"| {provider} | {seconds:.2f} |")
    if "directml" not in results:
        lines.append("")
        lines.append(
            "DirectMLは このonnxruntimeインストールでは利用できなかったため未計測"
            "(Windows実機での再実行が必要)。"
        )
    else:
        lines.append("")
        lines.append(
            "**注意**: 上記の `directml` 行は `onnxruntime.get_available_providers()` が "
            "`DmlExecutionProvider` を報告したことのみを根拠にしている。GPU/ドライバ起因で"
            "実際のセッション初期化が失敗した場合、`onnxruntime` は明示的なエラーを出さず"
            "CPUへ黙ってフォールバックすることがあるため、この行が実際にDirectMLで"
            "計測されたことは未確認(セッション作成後の `get_providers()` 等での"
            "個別確認が別途必要)。"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"[benchmark] wrote {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_path", type=Path)
    parser.add_argument(
        "--preset", choices=["fast", "standard", "high_quality"], default="standard"
    )
    parser.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS, help="ウォームアップ後の計測回数(中央値を採用)"
    )
    parser.add_argument("--out", type=Path, default=None, help="結果をMarkdownで書き出す先")
    args = parser.parse_args()

    if not args.audio_path.exists():
        print(f"error: audio file not found: {args.audio_path}", file=sys.stderr)
        return 1
    if args.runs < 1:
        # `--runs 0` 以下だと `_measure` の `samples` が空になり、
        # `statistics.median([])` が素の StatisticsError を投げて終了してしまう
        # (#17-M1レビュー指摘の追加ラウンド)。
        print(f"error: --runs must be >= 1, got {args.runs}", file=sys.stderr)
        return 1

    results = run_benchmark(args.audio_path, args.preset, args.runs)
    if args.out is not None:
        _write_report(args.out, args.audio_path, args.preset, args.runs, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
