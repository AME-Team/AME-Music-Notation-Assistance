"""#16: Stage 1 音源分離パイプラインのテスト。"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.pipeline.separate import (
    audio_fingerprint,
    params_hash,
    resolve_model,
    run_separation,
)


def test_resolve_model_maps_all_three_presets() -> None:
    assert resolve_model("fast") == "htdemucs"
    assert resolve_model("standard") == "htdemucs_6s"
    assert resolve_model("high_quality") == "htdemucs_ft"


def test_resolve_model_rejects_unknown_preset() -> None:
    with pytest.raises(ValueError, match="unknown preset"):
        resolve_model("ultra")


def test_params_hash_is_stable_and_sensitive_to_params() -> None:
    a = params_hash(model="htdemucs_6s", execution_provider="cpu")
    b = params_hash(model="htdemucs_6s", execution_provider="cpu")
    c = params_hash(model="htdemucs_6s", execution_provider="directml")
    assert a == b
    assert a != c


def test_params_hash_is_sensitive_to_audio_fingerprint() -> None:
    """入力音源が変わった(将来の再アップロード等)場合、古いステムを使い回さないための保証。"""
    a = params_hash(
        model="htdemucs_6s", execution_provider="cpu", audio_fingerprint_value="v1"
    )
    b = params_hash(
        model="htdemucs_6s", execution_provider="cpu", audio_fingerprint_value="v2"
    )
    assert a != b


def test_audio_fingerprint_changes_when_file_is_modified(tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    path.write_bytes(b"a" * 100)
    first = audio_fingerprint(path)

    path.write_bytes(b"b" * 200)  # サイズを変えて更新
    second = audio_fingerprint(path)

    assert first != second


def test_audio_fingerprint_changes_when_content_differs_at_same_size_and_mtime(
    tmp_path: Path,
) -> None:
    """回帰(#21-M1レビュー指摘): rsync等でサイズ+mtimeを保持したままコピーされても、

    内容が違えば指紋も変わるべき(サイズ+mtimeのみだと将来の原曲差し替え機能で
    古いステム/beatmapを誤って使い回してしまう)。
    """
    path = tmp_path / "source.wav"
    path.write_bytes(b"a" * 100)
    first = audio_fingerprint(path)

    stat_before = path.stat()
    path.write_bytes(b"z" * 100)  # 同じサイズ・違う内容
    os.utime(
        path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns)
    )  # mtimeも揃える
    second = audio_fingerprint(path)

    assert first != second


@pytest.mark.slow
def test_run_separation_produces_six_stems(tmp_path: Path) -> None:
    """実際に demucs-onnx (htdemucs_6s) を合成音声に対して実行する(#16の実配管検証)。"""
    sr = 44100
    duration = 3.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    mix = 0.3 * np.sin(2 * np.pi * 80 * t) + 0.2 * np.sin(2 * np.pi * 440 * t)
    mix = (mix / np.max(np.abs(mix))).astype(np.float32)
    audio_path = tmp_path / "synthetic.wav"
    sf.write(audio_path, np.stack([mix, mix], axis=-1), sr)

    output_dir = tmp_path / "stems"
    written = run_separation(
        audio_path, output_dir, preset="standard", execution_provider="cpu"
    )

    assert set(written) == {"drums", "bass", "other", "vocals", "guitar", "piano"}
    for path in written.values():
        assert path.exists()
        info = sf.info(str(path))
        assert info.samplerate == 44100
        assert info.subtype == "FLOAT"
