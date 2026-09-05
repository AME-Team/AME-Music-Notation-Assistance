"""Stage 2: ビート・ダウンビート・拍子推定(#18, §6 Stage 2)。

入力は原曲ミックス(分離前)。`beat-this` はCPU推論に対応し、ビートとダウンビートを
同時に取得できるが拍子は出力しないため、`time_signature.py` で自前導出する(#19)。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.pipeline.time_signature import derive_time_signatures, downbeat_indices

# `beat_this.inference.File2Beats` の呼び出し面(テストでモックしやすいよう抽象化)。
BeatTracker = Callable[[str], tuple[Iterable[float], Iterable[float]]]


@dataclass(frozen=True)
class BeatmapResult:
    beats: list[dict]
    downbeats_sec: list[float]
    time_signatures: list[dict]
    tempo_map: list[dict]
    confidence: float

    def to_dict(self) -> dict:
        return {
            "beats": self.beats,
            "downbeats_sec": self.downbeats_sec,
            "time_signatures": self.time_signatures,
            "tempo_map": self.tempo_map,
            "confidence": self.confidence,
        }


def _assign_bars(beats_sec: list[float], downbeats_sec: list[float]) -> list[dict]:
    """各ビートに小節番号(bar)と小節内位置(beat_in_bar)を割り当てる。

    ダウンビートの照合は `time_signature.downbeat_indices` を使い、
    `count_beats_per_bar` と判定基準(許容誤差)を一致させる(#19レビュー指摘:
    以前は `round(t, 6)` の厳密一致を使っており、モデル出力の浮動小数の僅かな
    差でこちらとあちらの小節境界が食い違いうった)。

    先頭ダウンビートより前に存在するビート(ピックアップ/アウフタクト)は
    `bar=0` として明示的に区別する。`count_beats_per_bar` も先頭ダウンビート
    より前のビートを数えないため、両者は一致する(bar=0はtime_signaturesの
    対象外)。
    """
    indices = set(downbeat_indices(beats_sec, downbeats_sec))
    first_downbeat_index = min(indices) if indices else 0

    result: list[dict] = []
    bar = 0
    beat_in_bar = 0
    for i, t in enumerate(beats_sec):
        if i < first_downbeat_index:
            result.append({"time_sec": t, "beat_in_bar": 0, "bar": 0})
            continue
        if i == first_downbeat_index:
            bar, beat_in_bar = 1, 1
        elif i in indices:
            bar += 1
            beat_in_bar = 1
        else:
            beat_in_bar += 1
        result.append({"time_sec": t, "beat_in_bar": beat_in_bar, "bar": bar})
    return result


def _tempo_map(beats_sec: list[float], bar_by_beat: list[dict]) -> list[dict]:
    """連続するビート間隔から瞬間BPMを算出し、ビート位置ごとに記録する。

    bar=0(先頭ダウンビートより前のピックアップ拍)のエントリは出力しない。
    §6のスキーマは `tempo_map[].bar` を小節番号として定義しており、フロント側が
    暗黙に `bar >= 1` を前提にする可能性がある(#19レビュー指摘)。
    """
    tempo_map: list[dict] = []
    for i, entry in enumerate(bar_by_beat):
        if i == 0 or entry["bar"] == 0:
            continue
        interval = beats_sec[i] - beats_sec[i - 1]
        if interval <= 0:
            continue
        bpm = round(60.0 / interval, 2)
        tempo_map.append({"bar": entry["bar"], "beat": float(entry["beat_in_bar"]), "bpm": bpm})
    return tempo_map


def _confidence(beats_sec: list[float]) -> float:
    """beat-this自体は信頼度スコアを公開していないため、ビート間隔の一貫性を代理指標とする。

    間隔のばらつき(変動係数)が小さいほど1.0に近い値を返す。テンポが安定した曲ほど
    高スコアになるという意味での近似値であり、モデル自体の確信度ではない。
    """
    if len(beats_sec) < 3:
        return 0.0
    intervals = [b - a for a, b in zip(beats_sec, beats_sec[1:], strict=False)]
    mean = sum(intervals) / len(intervals)
    if mean <= 0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in intervals) / len(intervals)
    coefficient_of_variation = (variance**0.5) / mean
    return max(0.0, min(1.0, 1.0 - coefficient_of_variation))


def build_beatmap(beats_sec: list[float], downbeats_sec: list[float]) -> BeatmapResult:
    """beat-thisの生出力(ビート・ダウンビート時刻)からbeatmap.jsonの中身を組み立てる。

    ML推論(`file2beats`呼び出し)と分離してあるため、ここは決定論的にテストできる。
    """
    bar_by_beat = _assign_bars(beats_sec, downbeats_sec)
    time_signatures = derive_time_signatures(downbeats_sec, beats_sec)
    return BeatmapResult(
        beats=bar_by_beat,
        downbeats_sec=list(downbeats_sec),
        time_signatures=[
            {"bar": ts.bar, "numerator": ts.numerator, "denominator": ts.denominator}
            for ts in time_signatures
        ],
        tempo_map=_tempo_map(beats_sec, bar_by_beat),
        confidence=_confidence(beats_sec),
    )


def run_beat_estimation(audio_path: str, *, tracker: BeatTracker | None = None) -> BeatmapResult:
    """`beat-this`を実行しbeatmap.jsonの中身を返す(#18)。

    `tracker`を渡すとテストでモックできる(実際のモデル推論を待たずに配管を検証する)。
    """
    if tracker is None:
        from beat_this.inference import File2Beats

        tracker = File2Beats(checkpoint_path="final0", device="cpu", dbn=False)

    beats, downbeats = tracker(audio_path)
    return build_beatmap([float(b) for b in beats], [float(d) for d in downbeats])
