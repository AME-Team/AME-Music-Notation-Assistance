"""異名同音・音名変換(#23, #26)。外部ライブラリに依存しない純粋関数群。

PR1(#23)時点では Score IR の不変条件検証(V-4/V-5, §9)に必要な最小限の
ピッチクラス/MIDI変換のみを提供する。「調号から異名同音表記を機械的に決定する」
L0(#26)の本体アルゴリズムは PR2 で追加する。
"""

from __future__ import annotations

from app.domain.score import Spelling

# MusicXMLの自然音(alter=0)ごとのピッチクラス(Cを0とする半音数)。
PITCH_CLASS_BY_STEP: dict[str, int] = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}


def spelling_pitch_class(spelling: Spelling) -> int:
    """`spelling` が表す実際のピッチクラス(0-11)。`alter` によるオクターブを

    跨ぐ変化(例: Cb → ピッチクラス11=Bと同じ)も正しく畳み込む。
    """
    return (PITCH_CLASS_BY_STEP[spelling.step] + spelling.alter) % 12


def spelling_to_midi(spelling: Spelling) -> int:
    """`spelling`(step+alter+octave)が表す絶対MIDI番号。

    MusicXMLのオクターブ表記(octave=4 が中央ハ=MIDI60を含むオクターブ)に従う。
    `alter` によってオクターブ境界を跨ぐ場合(例: B#4 は実質C5)も、
    自然音のピッチクラス+alterをそのまま足すことで正しいMIDI番号になる
    (畳み込みはしない。畳み込むと `spelling_pitch_class` と不整合になるため、
    こちらは意図的に生の値を使う)。
    """
    natural_pitch_class = PITCH_CLASS_BY_STEP[spelling.step]
    return (spelling.octave + 1) * 12 + natural_pitch_class + spelling.alter


def is_spelling_consistent_with_midi(spelling: Spelling, midi: int) -> bool:
    """V-4(ピッチクラス一致)+V-5(オクターブ整合)の両方を1回で検証する(§9)。

    V-4だけ(`spelling_pitch_class(spelling) == midi % 12`)を見ると、
    B#4(ピッチクラス0, 実際はMIDI60=C5相当)のような「ピッチクラスは合うが
    オクターブがずれる」表記を誤って許容してしまう。`spelling_to_midi` による
    絶対値比較まで行うことで、B#/Cb等の境界ケースも含めて正しく検証できる。
    """
    return spelling_to_midi(spelling) == midi
