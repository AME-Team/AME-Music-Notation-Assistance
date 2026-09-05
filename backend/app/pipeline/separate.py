"""Stage 1: 音源分離(#16, §6 Stage 1)。

`demucs-onnx` を採用する(PyTorch版 `demucs` ではない)。理由は推論時にPyTorch不要で
依存が軽いこと、Execution Provider の切替だけでCPU/DirectMLを跨げること(NFR-08)。

`demucs_onnx.separate()` はファイル書き出しも担えるが、内部の `write_wav()` は
16bit PCM 固定で書き出す。設計書は32bit floatを要求する(§6)ため、ここでは
`output_dir=None` で配列を受け取り、自前で `soundfile` を使って書き出す。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import soundfile as sf

SAMPLE_RATE = 44100

# #16: UIに露出する3プリセット→モデルのマッピング(設計書§6 Stage1の表)。
PRESET_TO_MODEL: dict[str, str] = {
    "fast": "htdemucs",  # 4ステム・単一ファイル。6ステムより高速
    "standard": "htdemucs_6s",  # 既定。ピアノ/ギターを個別に取れる
    "high_quality": "htdemucs_ft",  # 4ステムbag。最高品質・最も遅い
}

ExecutionProviderChoice = Literal["auto", "cpu", "directml"]

_EP_TO_ONNX_PROVIDERS: dict[str, list[str] | str] = {
    "auto": "auto",
    "cpu": ["CPUExecutionProvider"],
    # DirectMLが利用できない環境ではonnxruntime側がCPUへフォールバックする。
    "directml": ["DmlExecutionProvider", "CPUExecutionProvider"],
}


def resolve_model(preset: str) -> str:
    if preset not in PRESET_TO_MODEL:
        raise ValueError(f"unknown preset: {preset!r} (choices: {sorted(PRESET_TO_MODEL)})")
    return PRESET_TO_MODEL[preset]


def resolve_onnx_providers(execution_provider: str) -> list[str] | str:
    """`execution_provider` をonnxruntimeのproviderリストへ解決する。

    API層は `params` を未検証のままWorkerへ渡すため、不正な値は
    `resolve_model` と対称に分かりやすい `ValueError` にしておく(#21レビュー
    指摘: 以前は `_EP_TO_ONNX_PROVIDERS[...]` の素の `KeyError` になっており、
    Workerが「worker error: 'xxx'」としか出力せずジョブが失敗していた)。
    """
    if execution_provider not in _EP_TO_ONNX_PROVIDERS:
        raise ValueError(
            f"unknown execution_provider: {execution_provider!r} "
            f"(choices: {sorted(_EP_TO_ONNX_PROVIDERS)})"
        )
    return _EP_TO_ONNX_PROVIDERS[execution_provider]


_FINGERPRINT_EDGE_BYTES = 4096


def audio_fingerprint(audio_path: Path) -> str:
    """入力音源の同一性判定用の軽量な指紋。内容全体は読まない。

    サイズ+更新時刻に加え、先頭・末尾それぞれ最大4KiBのハッシュも含める
    (#21-M1レビュー指摘の追加ラウンド)。サイズ+mtimeのみだと、将来の原曲
    差し替え機能で rsync 等サイズとmtimeを保持したままコピーするツールを
    使った場合、内容が変わっていてもスキップ判定が古いステム/beatmapを
    使い回してしまう。ファイル全体は読まず先頭・末尾のみに留めることで、
    「軽量な指紋」という設計意図(§6)は維持する。

    既知の限界: サイズ・mtimeを保持したまま**ファイル中間部のみ**を書き換える
    (先頭・末尾4KiBの範囲外だけを改変する)ケースは検出できない。head/tail方式は
    「サイズ+mtimeを保持するコピーツール」というよくある実運用シナリオを主に
    想定した設計であり、任意のバイト単位改変に対する暗号学的な完全性検証では
    ない。より厳密な検証が必要になった場合はファイル全体のハッシュ化が必要。
    """
    stat = audio_path.stat()
    size = stat.st_size
    with audio_path.open("rb") as f:
        head = f.read(_FINGERPRINT_EDGE_BYTES)
        if size > _FINGERPRINT_EDGE_BYTES:
            f.seek(max(size - _FINGERPRINT_EDGE_BYTES, 0))
            tail = f.read(_FINGERPRINT_EDGE_BYTES)
        else:
            tail = b""
    edge_hash = hashlib.sha256(head + tail).hexdigest()
    return f"{size}:{stat.st_mtime_ns}:{edge_hash}"


def params_hash(
    *,
    model: str,
    execution_provider: str,
    audio_fingerprint_value: str = "",
    package_version: str = "",
) -> str:
    """§6: パラメータのハッシュを出力メタに記録し、入力・パラメータ不変ならスキップする。

    `audio_fingerprint_value` を含めない場合、将来的に原曲が差し替えられても
    (現状は1プロジェクト1音源で差し替え経路が無いが)スキップ判定が古いステムを
    使い回してしまう。`run_separate_stage` は `audio_fingerprint()` の結果を渡す。

    `package_version`(`demucs-onnx`のインストール済みバージョン)も同様の理由で
    含める: パッケージを更新しても同じモデル名・EPのままならスキップされ、
    古いバージョンで生成したステムを使い回してしまう(NFR-11のトレーサビリティと
    矛盾する。beatステージが`beat-this`のバージョンをハッシュに含めるのと対称)。
    """
    payload = json.dumps(
        {
            "model": model,
            "execution_provider": execution_provider,
            "audio_fingerprint": audio_fingerprint_value,
            "package_version": package_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_separation(
    audio_path: Path,
    output_dir: Path,
    *,
    preset: str = "standard",
    execution_provider: ExecutionProviderChoice = "auto",
) -> dict[str, Path]:
    """`demucs-onnx` でステム分離を実行し、`stems/{name}.wav`(32bit float)を書き出す。

    戻り値はステム名 → 書き出したファイルパスの辞書。
    """
    import demucs_onnx

    model = resolve_model(preset)
    providers = resolve_onnx_providers(execution_provider)

    stems = demucs_onnx.separate(
        str(audio_path),
        output_dir=None,  # 自前で32bit floatとして書き出すため、内蔵の書き出しは使わない
        model=model,
        providers=providers,
        verbose=False,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, audio in stems.items():
        path = output_dir / f"{name}.wav"
        _write_wav_atomically(path, audio.T, SAMPLE_RATE)
        written[name] = path
    return written


def _write_wav_atomically(path: Path, audio, sample_rate: int) -> None:
    """書き込み途中のファイルを配信APIが読んでしまわないよう、一時ファイル経由で置き換える。

    分離ジョブ(別プロセスのWorker)とメディア配信API(`/audio/stems/{name}`等)は
    同時に動きうる。`sf.write` で直接 `path` に書くと、書き込み中に読み取りリクエストが
    来た場合に不完全なWAVを返してしまう。同一ディレクトリ内で一時ファイルに書いてから
    `os.replace`(POSIX/Windowsともにアトミック)でリネームすることで、読み取り側は
    常に「書き込み前の状態」か「書き込み完了後の状態」のどちらかしか観測しない。
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        # 一時ファイルの拡張子は .tmp のため、soundfileがファイル名からフォーマットを
        # 推測できない。format="WAV" を明示する(拡張子推測に頼らない)。
        sf.write(str(tmp_path), audio, sample_rate, subtype="FLOAT", format="WAV")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
