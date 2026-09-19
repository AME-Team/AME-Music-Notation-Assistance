"""コード進行の自動推定(#57, FR-16, §8.4, §17 Q-3)。

Score IR 内の全パート(ピアノ・ベース・ボーカル等)のノート分布とベース音から、
小節・拍ごとのコード進行(ChordEntry)を決定論的に推定する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.score import ChordEntry, ScoreIR
from app.pipeline.refine.key_estimation import estimate_key
from app.pipeline.time_signature import tick_to_bar_beat, time_signature_at_bar

if TYPE_CHECKING:
    from app.domain.score import Note

# 代表的なコード構成音テンプレート(ルートからの半音オフセット)
CHORD_TEMPLATES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),  # Major triad
    "m": (0, 3, 7),  # Minor triad
    "7": (0, 4, 7, 10),  # Dominant 7th
    "maj7": (0, 4, 7, 11),  # Major 7th
    "m7": (0, 3, 7, 10),  # Minor 7th
    "dim": (0, 3, 6),  # Diminished triad
    "aug": (0, 4, 8),  # Augmented triad
    "sus4": (0, 5, 7),  # Suspended 4th
    "m7b5": (0, 3, 6, 10),  # Half-diminished 7th
}

# 調号に応じた音名表記(シャープ系 / フラット系)
SHARP_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_PITCH_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def get_pitch_name(pitch_class: int, fifths: int = 0) -> str:
    pc = pitch_class % 12
    return FLAT_PITCH_NAMES[pc] if fifths < 0 else SHARP_PITCH_NAMES[pc]


def estimate_chord_from_notes(
    notes: list[tuple[int, float, bool]],  # list of (midi, duration_weight, is_bass_part)
    fifths: int = 0,
) -> tuple[str, float] | None:
    """ノート一覧から1つのコードシンボルと信頼度を推定する。

    notes が空または全重みが 0 の場合は None を返す。
    """
    if not notes:
        return None

    chroma = [0.0] * 12
    lowest_midi = 999
    for midi, weight, is_bass in notes:
        if weight <= 0:
            continue
        # ベースパートの音、または低音域(MIDI < 50)はベース音として重み付け
        boost = 2.0 if (is_bass or midi < 48) else 1.0
        chroma[midi % 12] += weight * boost
        if midi < lowest_midi:
            lowest_midi = midi

    total = sum(chroma)
    if total <= 0:
        return None

    bass_pc = lowest_midi % 12
    best_score = -999.0
    best_root = 0
    best_typ = ""

    for r in range(12):
        for typ, offsets in CHORD_TEMPLATES.items():
            in_pcs = set((r + o) % 12 for o in offsets)
            present_pcs = sum(1 for p in in_pcs if chroma[p] > 0.05 * total)
            missing_pcs = len(in_pcs) - present_pcs
            e_in = sum(chroma[p] for p in in_pcs)
            e_out = sum(chroma[p] for p in set(range(12)) - in_pcs)

            # 構成音の一致度 - 非構成音ペナルティ
            score = (e_in - 0.7 * e_out) / total
            # 構成音が鳴っていない場合のペナルティ(トライアドと7thの誤判定防止)
            score -= missing_pcs * 0.25
            # 最低音(ベース)がルートと一致する場合は強いボーナス
            if bass_pc == r:
                score += 0.35

            if score > best_score:
                best_score = score
                best_root = r
                best_typ = typ

    root_name = get_pitch_name(best_root, fifths)
    symbol = f"{root_name}{best_typ}"

    # スラッシュコード(オンコード)判定:
    # 最低音がルートと異なり、かつ全体の20%以上のエネルギーを占めている場合
    if bass_pc != best_root and chroma[bass_pc] >= 0.2 * total:
        bass_name = get_pitch_name(bass_pc, fifths)
        symbol = f"{symbol}/{bass_name}"

    # 信頼度の計算: 構成音カバレッジと構成音充足率から算出
    in_pcs = set((best_root + o) % 12 for o in CHORD_TEMPLATES[best_typ])
    coverage = sum(chroma[p] for p in in_pcs) / total
    present_pcs = sum(1 for p in in_pcs if chroma[p] > 0.05 * total)
    completeness = present_pcs / len(in_pcs)

    confidence = max(0.1, min(1.0, coverage * 0.6 + completeness * 0.4))
    return symbol, round(confidence, 2)


def estimate_chords_for_score(score: ScoreIR) -> list[ChordEntry]:
    """ScoreIRの全パートのノートから小節・拍ごとのコード進行を推定する。"""
    if not score.parts:
        return []

    # 調号(fifths)を決定
    fifths = 0
    if score.key_signatures:
        fifths = score.key_signatures[0].fifths
    else:
        # パート全体のピッチクラスから推定
        all_pcs = [n.midi % 12 for p in score.parts for n in p.notes if n.status != "deleted"]
        if all_pcs:
            fifths = estimate_key(all_pcs).fifths

    time_signatures = [ts.model_dump(mode="json") for ts in score.time_signatures]
    divisions = score.divisions

    # 全ノートを小節・拍にマッピング
    # (part_id, note, bar, beat, duration_beat)
    annotated_notes: list[tuple[str, Note, int, float, float]] = []
    max_bar = 1

    for part in score.parts:
        for note in part.notes:
            if note.status == "deleted":
                continue
            if note.onset_tick is not None:
                bar, beat = tick_to_bar_beat(
                    note.onset_tick, time_signatures=time_signatures, divisions=divisions
                )
                _, denominator = time_signature_at_bar(time_signatures, bar)
                pulse_ticks = divisions * 4 / denominator
                duration_beat = (note.duration_tick or divisions) / pulse_ticks
            else:
                # クオンタイズ前フォールバック
                # 4/4 120bpm 想定 (0.5秒 = 1拍)
                beat_float = note.onset_sec / 0.5
                bar = int(beat_float // 4) + 1
                beat = (beat_float % 4) + 1.0
                duration_beat = max(0.25, note.duration_sec / 0.5)

            annotated_notes.append((part.id, note, bar, beat, duration_beat))
            if bar > max_bar:
                max_bar = bar

    if not annotated_notes:
        return []

    entries: list[ChordEntry] = []

    for bar in range(1, max_bar + 1):
        num, den = time_signature_at_bar(time_signatures, bar)
        bar_beats = float(num)

        # この小節に属するノート(小節内で開始するか、小節内にまたがっているノート)
        bar_notes = [an for an in annotated_notes if an[2] == bar]
        if not bar_notes:
            continue

        # 4拍以上の場合は2分割(前半・後半)でコード変化を検出
        if bar_beats >= 4.0:
            split_beat = 1.0 + (bar_beats / 2.0)
            notes_first_half: list[tuple[int, float, bool]] = []
            notes_second_half: list[tuple[int, float, bool]] = []

            for part_id, note, _, beat, dur in bar_notes:
                is_bass = "bass" in part_id.lower()
                weight = min(dur, bar_beats)
                if beat < split_beat:
                    notes_first_half.append((note.midi, weight, is_bass))
                else:
                    notes_second_half.append((note.midi, weight, is_bass))

            chord1 = estimate_chord_from_notes(notes_first_half, fifths)
            chord2 = estimate_chord_from_notes(notes_second_half, fifths)

            if chord1 and chord2:
                if chord1[0] == chord2[0]:
                    # 前半と後半で同一コードなら小節頭に1つ
                    entries.append(
                        ChordEntry(bar=bar, beat=1.0, symbol=chord1[0], confidence=chord1[1])
                    )
                else:
                    # 異なるコードなら前半と後半に2つ
                    entries.append(
                        ChordEntry(bar=bar, beat=1.0, symbol=chord1[0], confidence=chord1[1])
                    )
                    entries.append(
                        ChordEntry(bar=bar, beat=split_beat, symbol=chord2[0], confidence=chord2[1])
                    )
            elif chord1:
                entries.append(
                    ChordEntry(bar=bar, beat=1.0, symbol=chord1[0], confidence=chord1[1])
                )
            elif chord2:
                entries.append(
                    ChordEntry(bar=bar, beat=split_beat, symbol=chord2[0], confidence=chord2[1])
                )
        else:
            # 3拍以下なら小節全体で1コード
            notes_all = [
                (note.midi, min(dur, bar_beats), "bass" in part_id.lower())
                for part_id, note, _, _, dur in bar_notes
            ]
            chord = estimate_chord_from_notes(notes_all, fifths)
            if chord:
                entries.append(ChordEntry(bar=bar, beat=1.0, symbol=chord[0], confidence=chord[1]))

    return entries
