"""コード進行の自動推定(#57, FR-16, §8.4, §17 Q-3)。

Score IR 内の全パート(ピアノ・ベース・ボーカル等)のノート分布とベース音から、
小節・拍ごとのコード進行(ChordEntry)を決定論的に推定する。
"""

from __future__ import annotations

from app.domain.pitch import midi_to_spelling
from app.domain.score import ChordEntry, ScoreIR
from app.pipeline.refine.key_estimation import estimate_key
from app.pipeline.time_signature import time_signature_at_bar

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


def get_pitch_name(pitch_class: int, fifths: int = 0) -> str:
    """ピッチクラス(0〜11)を調号(fifths)に応じた音名文字列に変換する。

    `app.domain.pitch.midi_to_spelling` を再利用し、音名ロジックの一貫性を保つ。
    """
    spelling = midi_to_spelling(60 + (pitch_class % 12), fifths=fifths)
    accidental = "#" * spelling.alter if spelling.alter > 0 else "b" * (-spelling.alter)
    return f"{spelling.step}{accidental}"


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
        # ベースパートの音、または低音域(MIDI < 48)はベース音として重み付け
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


def _sec_to_ticks(sec: float, tempo_map: list[dict], divisions: int) -> int:
    """秒から近似 tick を計算する(tempo_map を考慮)。"""
    bpm = 120.0
    if tempo_map:
        bpm = tempo_map[0].get("bpm", 120.0)
    ticks_per_sec = (bpm / 60.0) * divisions
    return max(0, int(sec * ticks_per_sec))


def estimate_chords_for_score(score: ScoreIR) -> list[ChordEntry]:
    """ScoreIRの全パートのノートから小節・拍ごとのコード進行を推定する。

    各ノートの発音区間 [onset_tick, onset_tick + duration_tick) と小節・半区間の
    重なり(overlap)を按分集計するため、持続音やタイで継続する和音も正確に反映される。
    """
    if not score.parts:
        return []

    # 調号(fifths)を決定
    fifths = 0
    if score.key_signatures:
        fifths = score.key_signatures[0].fifths
    else:
        all_pcs = [n.midi % 12 for p in score.parts for n in p.notes if n.status != "deleted"]
        if all_pcs:
            fifths = estimate_key(all_pcs).fifths

    time_signatures = [ts.model_dump(mode="json") for ts in score.time_signatures]
    tempo_map = [t.model_dump(mode="json") for t in score.tempo_map]
    divisions = score.divisions

    # 全ノートの (part_id, midi, start_tick, end_tick) を集約
    note_intervals: list[tuple[str, int, int, int]] = []
    max_tick = 0

    for part in score.parts:
        for note in part.notes:
            if note.status == "deleted":
                continue
            if note.onset_tick is not None:
                start_tick = note.onset_tick
                dur_tick = note.duration_tick or divisions
            else:
                start_tick = _sec_to_ticks(note.onset_sec, tempo_map, divisions)
                dur_tick = max(
                    divisions // 4, _sec_to_ticks(note.duration_sec, tempo_map, divisions)
                )

            end_tick = start_tick + dur_tick
            note_intervals.append((part.id, note.midi, start_tick, end_tick))
            if end_tick > max_tick:
                max_tick = end_tick

    if not note_intervals:
        return []

    # 小節境界の tick を計算
    # 各小節 m の [start_tick, end_tick) をマッピング
    bar_boundaries: list[tuple[int, int, int, int, int]] = []  # (bar, num, den, bar_start, bar_end)
    current_tick = 0
    bar = 1
    while current_tick < max_tick or bar == 1:
        num, den = time_signature_at_bar(time_signatures, bar)
        pulse_ticks = int(divisions * 4 / den)
        bar_ticks = pulse_ticks * num
        next_tick = current_tick + bar_ticks
        bar_boundaries.append((bar, num, den, current_tick, next_tick))
        current_tick = next_tick
        bar += 1

    entries: list[ChordEntry] = []

    for m_bar, num, den, bar_start, bar_end in bar_boundaries:
        bar_beats = float(num)
        pulse_ticks = int(divisions * 4 / den)

        # 4拍以上の場合は2分割(前半・後半)でコード変化を検出
        if bar_beats >= 4.0:
            split_beat = 1.0 + (bar_beats / 2.0)
            mid_tick = bar_start + int(pulse_ticks * (bar_beats / 2.0))

            notes_first_half: list[tuple[int, float, bool]] = []
            notes_second_half: list[tuple[int, float, bool]] = []

            for part_id, midi, s_tick, e_tick in note_intervals:
                is_bass = "bass" in part_id.lower()

                # 前半区間 [bar_start, mid_tick) との重なり
                ov1 = max(0, min(e_tick, mid_tick) - max(s_tick, bar_start))
                if ov1 > 0:
                    weight1 = ov1 / pulse_ticks
                    notes_first_half.append((midi, weight1, is_bass))

                # 後半区間 [mid_tick, bar_end) との重なり
                ov2 = max(0, min(e_tick, bar_end) - max(s_tick, mid_tick))
                if ov2 > 0:
                    weight2 = ov2 / pulse_ticks
                    notes_second_half.append((midi, weight2, is_bass))

            chord1 = estimate_chord_from_notes(notes_first_half, fifths)
            chord2 = estimate_chord_from_notes(notes_second_half, fifths)

            if chord1 and chord2:
                if chord1[0] == chord2[0]:
                    entries.append(
                        ChordEntry(bar=m_bar, beat=1.0, symbol=chord1[0], confidence=chord1[1])
                    )
                else:
                    entries.append(
                        ChordEntry(bar=m_bar, beat=1.0, symbol=chord1[0], confidence=chord1[1])
                    )
                    entries.append(
                        ChordEntry(
                            bar=m_bar, beat=split_beat, symbol=chord2[0], confidence=chord2[1]
                        )
                    )
            elif chord1:
                entries.append(
                    ChordEntry(bar=m_bar, beat=1.0, symbol=chord1[0], confidence=chord1[1])
                )
            elif chord2:
                entries.append(
                    ChordEntry(bar=m_bar, beat=split_beat, symbol=chord2[0], confidence=chord2[1])
                )
        else:
            # 3拍以下なら小節全体で重なりを集計
            notes_all: list[tuple[int, float, bool]] = []
            for part_id, midi, s_tick, e_tick in note_intervals:
                ov = max(0, min(e_tick, bar_end) - max(s_tick, bar_start))
                if ov > 0:
                    weight = ov / pulse_ticks
                    notes_all.append((midi, weight, "bass" in part_id.lower()))

            chord = estimate_chord_from_notes(notes_all, fifths)
            if chord:
                entries.append(
                    ChordEntry(bar=m_bar, beat=1.0, symbol=chord[0], confidence=chord[1])
                )

    return entries
