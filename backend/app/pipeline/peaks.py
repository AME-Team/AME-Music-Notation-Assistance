"""波形ピークデータの事前計算(#21, §11.6)。

数十MBのWAVをフロントにそのまま渡して解析させると初期表示が遅いため、
min/maxピークをブロック単位で事前計算し、軽量なJSONとして返す。
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

DEFAULT_BUCKETS = 1000
# 拡大表示用の解像度リクエスト(#169)の許容範囲。下限は「全曲を1点で描く」
# ような無意味な要求を弾くため、上限はJSONが肥大化しすぎないよう抑制する。
MIN_BUCKETS = 100
MAX_BUCKETS = 40000


def compute_peaks(audio_path: str, *, buckets: int = DEFAULT_BUCKETS) -> dict:
    audio, sample_rate = sf.read(audio_path, always_2d=True, dtype="float32")
    mono = audio.mean(axis=1)
    total_samples = len(mono)
    duration_sec = total_samples / sample_rate if sample_rate else 0.0

    if total_samples == 0:
        return {"duration_sec": 0.0, "sample_rate": sample_rate, "peaks": []}

    bucket_count = max(1, min(buckets, total_samples))
    edges = np.linspace(0, total_samples, bucket_count + 1, dtype=int)
    peaks: list[list[float]] = []
    for start, end in zip(edges, edges[1:], strict=False):
        chunk = mono[start:end] if end > start else mono[start : start + 1]
        peaks.append([round(float(chunk.min()), 4), round(float(chunk.max()), 4)])

    return {"duration_sec": round(duration_sec, 3), "sample_rate": int(sample_rate), "peaks": peaks}
