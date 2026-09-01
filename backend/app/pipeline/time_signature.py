"""#19 Q-16: 拍子導出ロジックの自前実装。

`beat-this` はビートとダウンビートしか出力せず、拍子は出力しない(§6 Stage 2)。
ダウンビート間に含まれるビート数から拍子を導出する、外部ライブラリに依存しない
決定論的なロジックをここに置く。

Q-16 の判断ポイント: 3/4 と 6/8 は主拍(tactus)の数だけでは原理的に判別できない
ことがある(6/8 は速いテンポでは 2 拍/小節に感じられ、3/4 と見分けがつかない)。
本実装は「6拍/小節なら6/8」という経験則で割り切り、判別できない曲は
BeatGridEditor(#20)での手動指定を前提とする。
"""

from __future__ import annotations

from dataclasses import dataclass

# ダウンビート間のビート数(拍子の分子相当) → (numerator, denominator)。
# ここに無い値(8, 10, 11拍等)は `DEFAULT_SIGNATURE`(4/4)へフォールバックする
# (このフォールバックの是非自体がQ-16の未決事項であり、判別できない曲は
# BeatGridEditorでの手動指定を前提とする)。
_BEATS_PER_BAR_TO_SIGNATURE: dict[int, tuple[int, int]] = {
    1: (1, 4),
    2: (2, 4),
    3: (3, 4),
    4: (4, 4),
    5: (5, 4),
    6: (6, 8),  # 経験則: 6拍/小節は複合拍子(6/8)である可能性が高い(Q-16)
    7: (7, 8),
    9: (9, 8),
    12: (12, 8),
}
DEFAULT_SIGNATURE: tuple[int, int] = (4, 4)

# beat.py の _assign_bars と共有する許容誤差。ダウンビート時刻はビート時刻の
# いずれかと同一の値であるはず(beat-thisの出力・BeatGridEditorの補正いずれも)
# だが、モデル出力の浮動小数の僅かな差(丸め誤差)を吸収するために許容誤差を使う。
# 両モジュールで基準が異なると、beats[].bar と time_signatures[].bar の小節境界が
# 食い違うため、必ずこの定数・関数を共用すること。
TIME_MATCH_TOLERANCE_SEC = 1e-6


def find_close_index(times: list[float], value: float) -> int | None:
    """許容誤差付きで時刻に一致するインデックスを探す(beat.py/beatmap_edit.pyと共有)。"""
    for i, t in enumerate(times):
        if abs(t - value) < TIME_MATCH_TOLERANCE_SEC:
            return i
    return None


def downbeat_indices(beats_sec: list[float], downbeats_sec: list[float]) -> list[int]:
    """ダウンビート時刻に対応する `beats_sec` 内のインデックス列(昇順・重複無し)。

    ダウンビートは常にビート列のいずれかと同一時刻であるという前提(beat-thisの
    出力・build_beatmapの構築規則)のもとで、実際のビート配列上の位置に変換する。
    対応するビートが見つからないダウンビート(不整合な入力)は無視する。
    """
    indices = {idx for d in downbeats_sec if (idx := find_close_index(beats_sec, d)) is not None}
    return sorted(indices)


@dataclass(frozen=True)
class TimeSignature:
    bar: int
    numerator: int
    denominator: int


def count_beats_per_bar(downbeats_sec: list[float], beats_sec: list[float]) -> list[int]:
    """ダウンビートの区間ごとに含まれるビート数を数える。

    `beat.py::_assign_bars` と同じインデックスベースの照合(`downbeat_indices`)を
    使うことで、小節境界の判定基準を両モジュールで一致させる(浮動小数の丸め方式が
    ズレていると beats[].bar と time_signatures[].bar が食い違うため)。
    先頭ダウンビートより前のビート(ピックアップ/アウフタクト)は数えない
    (`_assign_bars` 側で bar=0 として扱うのと対称)。最後の小節は次のダウンビートが
    無いため、残り全てのビートを含める。
    """
    if not downbeats_sec:
        return []

    indices = downbeat_indices(beats_sec, downbeats_sec)
    if not indices:
        return []

    bounds = [*indices, len(beats_sec)]
    return [max(end - start, 1) for start, end in zip(bounds, bounds[1:], strict=False)]


def derive_time_signatures(
    downbeats_sec: list[float], beats_sec: list[float]
) -> list[TimeSignature]:
    """ダウンビート間隔から拍子を導出し、区間ごとの拍子変化のみを記録する。

    出力は §6 Stage2 の `time_signatures` フィールドのスキーマに準拠する:
    拍子が変化した小節番号でのみ新しいエントリを追加する(変化が無ければ1件のみ)。
    """
    beats_per_bar = count_beats_per_bar(downbeats_sec, beats_sec)
    signatures: list[TimeSignature] = []
    previous: tuple[int, int] | None = None
    for bar_index, count in enumerate(beats_per_bar, start=1):
        numerator, denominator = _BEATS_PER_BAR_TO_SIGNATURE.get(count, DEFAULT_SIGNATURE)
        current = (numerator, denominator)
        if current != previous:
            signatures.append(
                TimeSignature(bar=bar_index, numerator=numerator, denominator=denominator)
            )
            previous = current
    return signatures
