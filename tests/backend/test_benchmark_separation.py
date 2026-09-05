"""#17: 分離ベンチマークスクリプトのテスト(実モデル呼び出し以外のロジック)。

`backend/scripts/benchmark_separation.py` は `app` パッケージの外(通常のスクリプト)
にあるため、`importlib` でファイルパスから直接読み込む。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "scripts"
    / "benchmark_separation.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("benchmark_separation", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def benchmark_module():
    return _load_module()


def test_measure_discards_warmup_and_reports_median(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#17-M1レビュー指摘): 単発計測は初回のモデルダウンロード/ロードで歪む

    ため、ウォームアップ1回(計測に含めない)+ 複数回計測の中央値を採用する。
    """
    samples = iter([100.0, 1.0, 3.0, 2.0])  # 先頭がウォームアップ(捨てられる)
    call_count = 0

    def _fake_run_once(audio_path, preset, execution_provider, output_dir):
        nonlocal call_count
        call_count += 1
        return next(samples)

    monkeypatch.setattr(benchmark_module, "_run_once", _fake_run_once)

    result = benchmark_module._measure(tmp_path / "song.wav", "fast", "cpu", runs=3)

    assert call_count == 4  # ウォームアップ1回 + 計測3回
    assert result == 2.0  # [1.0, 3.0, 2.0] の中央値。100.0(ウォームアップ)は含まれない


def test_run_benchmark_always_measures_cpu(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark_module, "_detect_directml", lambda: False)
    monkeypatch.setattr(benchmark_module, "_measure", lambda *a, **k: 1.5)

    results = benchmark_module.run_benchmark(tmp_path / "song.wav", "fast")

    assert results == {"cpu": 1.5}


def test_run_benchmark_measures_directml_when_available(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark_module, "_detect_directml", lambda: True)
    calls = []

    def _fake_measure(audio_path, preset, execution_provider, runs):
        calls.append(execution_provider)
        return {"cpu": 1.0, "directml": 0.4}[execution_provider]

    monkeypatch.setattr(benchmark_module, "_measure", _fake_measure)

    results = benchmark_module.run_benchmark(tmp_path / "song.wav", "standard")

    assert results == {"cpu": 1.0, "directml": 0.4}
    assert calls == ["cpu", "directml"]


def test_write_report_includes_all_measured_providers(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark_module, "_onnxruntime_version", lambda: "1.28.0")
    out_path = tmp_path / "report.md"
    benchmark_module._write_report(
        out_path, tmp_path / "song.wav", "standard", 3, {"cpu": 12.3, "directml": 3.1}
    )

    content = out_path.read_text(encoding="utf-8")
    assert "htdemucs_6s" in content
    assert "12.3" in content or "12.30" in content
    assert "3.1" in content or "3.10" in content
    assert "1.28.0" in content
    assert "未計測" not in content  # 両方計測できた場合、未計測の注記は出ない


def test_write_report_notes_directml_result_is_unconfirmed(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#17-M1レビュー指摘): DirectMLが実際に使われたかは未確認である旨を明記する

    (GPU/ドライバ起因でセッション初期化が失敗すると、onnxruntimeがエラー無しで
    CPUへフォールバックしうるため)。
    """
    monkeypatch.setattr(benchmark_module, "_onnxruntime_version", lambda: "1.28.0")
    out_path = tmp_path / "report.md"
    benchmark_module._write_report(
        out_path, tmp_path / "song.wav", "standard", 3, {"cpu": 12.3, "directml": 3.1}
    )

    content = out_path.read_text(encoding="utf-8")
    assert "未確認" in content


def test_write_report_notes_directml_unmeasured(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark_module, "_onnxruntime_version", lambda: "1.28.0")
    out_path = tmp_path / "report.md"
    benchmark_module._write_report(
        out_path, tmp_path / "song.wav", "fast", 3, {"cpu": 5.0}
    )

    content = out_path.read_text(encoding="utf-8")
    assert "未計測" in content


def test_main_errors_on_non_positive_runs(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#17-M1レビュー指摘の追加ラウンド): --runs 0以下は statistics.median([])

    の素のStatisticsErrorで落ちる前に、usageエラーとして弾く。
    """
    audio_path = tmp_path / "song.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    monkeypatch.setattr(
        sys, "argv", ["benchmark_separation.py", str(audio_path), "--runs", "0"]
    )
    assert benchmark_module.main() == 1


def test_main_errors_on_missing_audio_file(
    benchmark_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["benchmark_separation.py", str(tmp_path / "missing.wav")]
    )
    assert benchmark_module.main() == 1
