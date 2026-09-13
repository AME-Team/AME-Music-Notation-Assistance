"""Stage 6: Score IR(JSON dict形状) → partitura `Score` の共有構築ロジック(#27)。

`musicxml.py`(MusicXML書き出し)と`midi.py`(FR-13 SMF書き出し)の両方が、
`partitura.save_musicxml`/`partitura.save_score_midi`に渡す前段として
この`build_score()`を共有する(#27-M2レビュー指摘: 実装を一箇所に集約し、
`ExportError`もここで一元定義して両モジュールから再利用できるようにする)。

スパイク検証で確認済みの partitura の挙動:

- `Part.add(obj, start, end)` で追加した`TimeSignature`/`KeySignature`から、
  `partitura.score.add_measures(part)` が小節を自動構築する
- `partitura.score.tie_notes(part)` が、小節境界をまたぐノートや標準的な
  音価に収まらないノートを、タイで結んだ複数ノートへ自動的に分割する
  (AMT検出+量子化由来の任意長ノートを正しく記譜するために必須)

`pipeline/` はdomain非依存の方針(`beat.py`/`quantize.py`と同様)のため、
入力はdomainモデルではなく Score IR と同じJSON形状の生dict
(`ScoreIR.model_dump(mode="json")`)で受け取る。呼び出し元(`api/export.py`)
がdomainモデルとの変換を担う。

**既知の制約**:
- Score IRの`Note.tie`(手動編集由来のタイ指定)は現時点では消費しない。
  M2時点でこのフィールドを書き込むステージが存在しない(quantize/L0は
  タイを一切生成しない)ため、`tie_notes()`による自動タイ(小節境界/音価
  起因)のみで完結する。手動タイ編集はM3以降の課題とする
- MIDI書き出し(`midi.py`)は`midi_program`を生成後の`MidiFile`へ
  program changeとして後付けで反映する(`partitura.save_score_midi`の
  Part経由の書き出し経路自体にはprogram changeを挿入する機構が無いため、
  `midi.py`の`_apply_midi_programs`が担う)。ただし単一声部パートは
  partituraの既定でMIDIチャンネル0に割り当てられるため、複数パート
  (かつ単一声部)を持つスコアではチャンネルが衝突しうる。M2はピアノ1パート
  のみのためこの制約は顕在化しない(詳細は`midi.py`のdocstring参照)
"""

from __future__ import annotations

from typing import Any

from app.pipeline.time_signature import bar_start_ticks, time_signature_at_bar

DEFAULT_DIVISIONS = 480


class ExportError(ValueError):
    """エクスポート前提条件エラー(例: 量子化/L0未実行でtick/spellingが未設定)。"""


def _max_referenced_bar(*entry_lists: list[dict]) -> int:
    max_bar = 1
    for entries in entry_lists:
        for entry in entries:
            max_bar = max(max_bar, entry["bar"])
    return max_bar


def _tempo_tick_and_quarter_bpm(
    tempo_entry: dict, *, time_signatures: list[dict], bar_starts: dict[int, int], divisions: int
) -> tuple[int, float]:
    """tempo_mapの1エントリ`{bar, beat, bpm}`を、絶対tickと

    MusicXML規約(四分音符あたりの拍数)のbpmへ変換する。

    `beat`/`bpm`はビート追跡の拍単位(拍子の分母が表す音符、例: 6/8なら
    8分音符)を基準にしており、四分音符単位ではない(#19で確立した事実:
    ビートトラッキングの「拍」は常に四分音符とは限らない)。そのため
    素朴に`(beat-1)*divisions`とすると分母が4以外の拍子で誤ったtick/bpmに
    なる。分母の1拍が四分音符何個分かを掛けて変換する。
    """
    numerator, denominator = time_signature_at_bar(time_signatures, tempo_entry["bar"])
    pulse_in_quarters = 4 / denominator
    pulse_ticks = divisions * pulse_in_quarters
    tick = bar_starts.get(tempo_entry["bar"], 0) + round((tempo_entry["beat"] - 1) * pulse_ticks)
    quarter_bpm = tempo_entry["bpm"] * pulse_in_quarters
    return tick, quarter_bpm


def _spelling_or_raise(note: dict, part_id: str) -> tuple[str, int | None, int]:
    """ノートの`spelling`を検証して`(step, alter, octave)`を返す(#27-M2レビュー指摘)。

    `spelling`自体が`None`なだけでなく、キー(`step`/`octave`)欠落もここで
    `ExportError`として明示的に弾く。呼び出し元(API層)が`ExportError`を
    422として扱う契約のため、`KeyError`をそのまま漏らして500にしない。
    """
    spelling = note.get("spelling")
    if not isinstance(spelling, dict) or "step" not in spelling or "octave" not in spelling:
        raise ExportError(
            f"note {note.get('id')} in part {part_id!r} has an invalid/missing spelling; "
            "run the quantize stage first"
        )
    return spelling["step"], spelling.get("alter") or None, spelling["octave"]


def build_score(score: dict[str, Any]) -> Any:  # noqa: ANN401 — partituraの型を外部公開しない
    """Score IRのJSON dict形状から partitura の `Score` を構築する(#27)。

    `musicxml.render_musicxml`/`midi.render_midi`(FR-13)の両方から共有される。
    """
    import partitura
    from partitura.score import Clef, KeySignature, Part, Score, SustainPedalDirection, Tempo
    from partitura.score import Note as PartituraNote
    from partitura.score import TimeSignature as PartituraTimeSignature

    divisions = score.get("divisions", DEFAULT_DIVISIONS)
    time_signatures = score.get("time_signatures", [])
    key_signatures = score.get("key_signatures", [])
    tempo_map = score.get("tempo_map", [])
    max_bar = _max_referenced_bar(time_signatures, key_signatures, tempo_map)
    bar_starts = bar_start_ticks(time_signatures, divisions, max_bar)

    parts = []
    for part_data in score.get("parts", []):
        part_id = part_data["id"]
        part = Part(part_id, part_data["name"], quarter_duration=divisions)

        for ts in time_signatures:
            tick = bar_starts.get(ts["bar"], 0)
            part.add(PartituraTimeSignature(ts["numerator"], ts["denominator"]), tick)
        for ks in key_signatures:
            tick = bar_starts.get(ks["bar"], 0)
            part.add(KeySignature(ks["fifths"], ks["mode"]), tick)
        for tempo_entry in tempo_map:
            tick, quarter_bpm = _tempo_tick_and_quarter_bpm(
                tempo_entry,
                time_signatures=time_signatures,
                bar_starts=bar_starts,
                divisions=divisions,
            )
            part.add(Tempo(quarter_bpm), tick)
        for clef in part_data.get("clefs", []):
            part.add(
                Clef(staff=clef["staff"], sign=clef["sign"], line=clef["line"], octave_change=0),
                0,
            )

        for note in part_data.get("notes", []):
            if note.get("status") == "deleted":
                continue
            onset_tick = note.get("onset_tick")
            duration_tick = note.get("duration_tick")
            if onset_tick is None or duration_tick is None:
                raise ExportError(
                    f"note {note.get('id')} in part {part_id!r} is missing "
                    "onset_tick/duration_tick; run the quantize stage first"
                )
            step, alter, octave = _spelling_or_raise(note, part_id)
            note_obj = PartituraNote(
                step=step,
                octave=octave,
                alter=alter,
                voice=note.get("voice", 1),
                staff=note.get("staff", 1),
                id=f"n{note['id']}",
            )
            part.add(note_obj, onset_tick, onset_tick + duration_tick)

        for pedal in part_data.get("pedals", []):
            start_tick, stop_tick = pedal.get("start_tick"), pedal.get("stop_tick")
            if start_tick is None or stop_tick is None:
                raise ExportError(
                    f"a pedal event in part {part_id!r} is missing "
                    "start_tick/stop_tick; run the quantize stage first"
                )
            part.add(SustainPedalDirection(line=True), start_tick, stop_tick)

        partitura.score.add_measures(part)
        partitura.score.tie_notes(part)
        parts.append(part)

    return Score(partlist=parts)
