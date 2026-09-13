"""ピッチクラス分布からの調推定(#26のL0、#38のL1チャンク文脈注入で共有)。

music21のKrumhansl-Schmuckler調推定は固定の統計アルゴリズムでありAIではない
(M2の完了条件に抵触しない、`baseline.py`と同じ判断)。`baseline.py`のL0は
`fifths`(int)のみを消費するが、#38のL1チャンク入力(設計書§7.3)は
`key_estimate`(例:`"F# major"`)/`key_confidence`(例:`0.72`)という文字列+
信頼度も必要とするため、共通のmusic21呼び出しをここに集約し、両者が同じ
調推定結果に基づくようにする(重複実装すると、L0とL1で異なる調を推定して
しまう食い違いのリスクがあるため)。
"""

from __future__ import annotations

from dataclasses import dataclass

MIDDLE_C = 60


@dataclass(frozen=True)
class KeyEstimate:
    fifths: int  # -7〜7
    label: str  # 例: "F# major", "Bb minor"(music21のハイフン表記"-"は"b"に変換)
    confidence: float  # music21のcorrelationCoefficientを[0.0, 1.0]にクランプ


_DEFAULT_ESTIMATE = KeyEstimate(fifths=0, label="C major", confidence=0.0)


def estimate_key(pitch_classes: list[int]) -> KeyEstimate:
    """ピッチクラス分布から調を推定する。ノートが1件も無ければハ長調(信頼度0)を返す。"""
    if not pitch_classes:
        return _DEFAULT_ESTIMATE

    import music21

    stream = music21.stream.Stream()
    for pitch_class in pitch_classes:
        # 調推定はピッチクラスの分布だけに依存するため、オクターブは任意
        # (すべて中央ハ寄りの1オクターブ内に詰めて渡す)。
        stream.append(music21.note.Note(MIDDLE_C + pitch_class))
    key = stream.analyze("key")

    tonic_name = key.tonic.name.replace("-", "b")
    confidence = max(0.0, min(1.0, key.correlationCoefficient))
    return KeyEstimate(
        fifths=int(key.sharps), label=f"{tonic_name} {key.mode}", confidence=confidence
    )
