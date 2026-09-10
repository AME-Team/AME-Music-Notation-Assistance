"""異名同音・音名変換(#23, #26)。外部ライブラリに依存しない純粋関数群。

Score IR の不変条件検証(V-4/V-5, §9)に必要なピッチクラス/MIDI変換に加え、
「調号から異名同音表記を機械的に決定する」L0(#26)本体アルゴリズムを提供する。
"""

from __future__ import annotations

from app.domain.score import PitchStep, Spelling

_STEPS: tuple[PitchStep, ...] = ("C", "D", "E", "F", "G", "A", "B")

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


# 調号(fifths)が全音階のどの音を変化させるかの順序(五度圏)。
_SHARP_ORDER = "FCGDAEB"
_FLAT_ORDER = "BEADGCF"


def _diatonic_spelling_by_pitch_class(fifths: int) -> dict[int, tuple[PitchStep, int]]:
    """調号`fifths`(-7〜7, 長調・短調どちらでも共通のピッチクラス集合)が示す

    全音階7音の (ピッチクラス -> (step, alter)) 対応。
    """
    altered_letters = _SHARP_ORDER[:fifths] if fifths >= 0 else _FLAT_ORDER[:-fifths]
    alter = 1 if fifths >= 0 else -1

    result: dict[int, tuple[PitchStep, int]] = {}
    for letter in _STEPS:
        note_alter = alter if letter in altered_letters else 0
        pitch_class = (PITCH_CLASS_BY_STEP[letter] + note_alter) % 12
        result[pitch_class] = (letter, note_alter)
    return result


def _octave_for_spelling(midi: int, step: PitchStep, alter: int) -> int:
    """`step`+`alter`がピッチクラス的に`midi`と整合する前提で、対応するoctaveを逆算する。"""
    return (midi - PITCH_CLASS_BY_STEP[step] - alter) // 12 - 1


def midi_to_spelling(midi: int, *, fifths: int = 0, prev_midi: int | None = None) -> Spelling:
    """MIDI番号を、調号`fifths`に沿った異名同音表記へ変換する(#26 L0)。

    全音階7音のピッチクラスは調号がそのまま決める。残り5音(半音階音)は、
    直前ノートからの半音進行(上行→シャープ側、下行→フラット側)があれば
    それを優先し、無ければ調号の既定方向(シャープ系の調ならシャープ、
    フラット系の調ならフラット)へ倒す(§7.4)。

    **既知の制約**: 半音階音は隣接する全音階音を1半音動かして表記を作るため、
    調号自体が変化させている音を打ち消す借用音(例: ト長調でのF#に対する
    ナチュラルF)を、機能和声的に正しい表記(F)ではなく不自然な異名同音
    (E#)として出す場合がある。和声的機能を考慮した高度な表記判断はL1(M4)の
    AI整音に委ねる、既知の簡略化。
    """
    pitch_class = midi % 12
    diatonic = _diatonic_spelling_by_pitch_class(fifths)

    if pitch_class in diatonic:
        step, alter = diatonic[pitch_class]
    else:
        prefer_sharp = fifths >= 0
        if prev_midi is not None:
            delta = midi - prev_midi
            if delta == 1:
                prefer_sharp = True
            elif delta == -1:
                prefer_sharp = False
        if prefer_sharp:
            lower_step, lower_alter = diatonic[(pitch_class - 1) % 12]
            step, alter = lower_step, lower_alter + 1
        else:
            upper_step, upper_alter = diatonic[(pitch_class + 1) % 12]
            step, alter = upper_step, upper_alter - 1

    return Spelling(step=step, alter=alter, octave=_octave_for_spelling(midi, step, alter))
