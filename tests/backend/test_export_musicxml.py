"""#27: Stage 6 MusicXML/MIDI書き出しのテスト。

partituraでの構築(タイの自動分割・小節構築・拍子/調号/テンポ変化)が
正しく動くことを、生成物を実際に再パースして検証する。`render_musicxml`/
`render_midi`はScore IRのJSON dict形状(ScoreIR.model_dump(mode="json")と
同じキー)を受け取るため、テストでも同形の辞書を直接組み立てる。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.pipeline.export.midi import render_midi
from app.pipeline.export.musicxml import ExportError, render_musicxml
from app.pipeline.export.musicxml import (
    _inject_score_instruments as inject_score_instruments,
)


def _note(
    id: int,
    onset_tick: int,
    duration_tick: int,
    *,
    midi: int = 60,
    step: str = "C",
    alter: int = 0,
    octave: int = 4,
    voice: int = 1,
    staff: int = 1,
    status: str = "active",
) -> dict:
    return {
        "id": id,
        "onset_tick": onset_tick,
        "duration_tick": duration_tick,
        "midi": midi,
        "velocity": 90,
        "spelling": {"step": step, "alter": alter, "octave": octave},
        "voice": voice,
        "staff": staff,
        "status": status,
        "provenance": "amt",
        "flags": [],
    }


def _piano_part(notes: list[dict], *, pedals: list[dict] | None = None) -> dict:
    return {
        "id": "piano",
        "name": "Piano",
        "midi_program": 0,
        "staves": 2,
        "clefs": [
            {"staff": 1, "sign": "G", "line": 2},
            {"staff": 2, "sign": "F", "line": 4},
        ],
        "notes": notes,
        "pedals": pedals or [],
    }


def _score(
    parts: list[dict],
    *,
    divisions: int = 480,
    time_signatures: list[dict] | None = None,
    key_signatures: list[dict] | None = None,
    tempo_map: list[dict] | None = None,
) -> dict:
    return {
        "divisions": divisions,
        "time_signatures": time_signatures
        or [{"bar": 1, "numerator": 4, "denominator": 4}],
        "key_signatures": key_signatures or [],
        "tempo_map": tempo_map or [],
        "parts": parts,
    }


def _parse(xml_bytes: bytes) -> ET.Element:
    return ET.fromstring(xml_bytes)


class TestRenderMusicxmlBasics:
    def test_divisions_is_480(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480)])])
        root = _parse(render_musicxml(score))
        assert root.findtext(".//divisions") == "480"

    def test_two_staves_and_clefs_are_emitted(self) -> None:
        score = _score(
            [_piano_part([_note(1, 0, 480, staff=1), _note(2, 0, 480, staff=2)])]
        )
        root = _parse(render_musicxml(score))
        assert root.findtext(".//staves") == "2"
        clefs = root.findall(".//clef")
        assert len(clefs) == 2

    def test_part_name_is_present(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480)])])
        root = _parse(render_musicxml(score))
        assert root.findtext(".//score-part/part-name") == "Piano"

    def test_score_instrument_and_midi_instrument_are_injected(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480)])])
        root = _parse(render_musicxml(score))
        score_part = root.find(".//score-part")
        assert score_part is not None
        assert score_part.findtext("score-instrument/instrument-name") == "Piano"
        assert (
            score_part.findtext("midi-instrument/midi-program") == "1"
        )  # 0 + 1(1始まり)

    def test_instrument_injection_raises_export_error_when_score_part_not_found(
        self,
    ) -> None:
        """回帰(#27-M2レビュー3巡目): `<score-part id="...">`のパターンに一致しない

        (partituraの出力形式が変わった等の)場合、無言でスキップせず
        `ExportError`を送出する。
        """
        xml_str = '<score-partwise><part-list><score-part id="other"/></part-list></score-partwise>'
        with pytest.raises(ExportError):
            inject_score_instruments(
                xml_str, [{"id": "piano", "name": "Piano", "midi_program": 0}]
            )

    def test_part_name_with_backslash_does_not_corrupt_the_regex_replacement(
        self,
    ) -> None:
        """回帰(#27-M2レビュー2巡目): パート名に`\\1`等のバックスラッシュ

        シーケンスが含まれると、置換文字列を直接渡すre.subnでは後方参照として
        誤解釈され出力が壊れたり例外になる。置換関数を渡すことで回避する。
        """
        part = _piano_part([_note(1, 0, 480)])
        part["name"] = r"Piano \1 \g<0>"
        score = _score([part])

        root = _parse(render_musicxml(score))
        score_part = root.find(".//score-part")
        assert score_part is not None
        assert (
            score_part.findtext("score-instrument/instrument-name") == r"Piano \1 \g<0>"
        )

    def test_sharp_accidental_is_emitted(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480, step="F", alter=1, midi=66)])])
        root = _parse(render_musicxml(score))
        assert root.findtext(".//note/pitch/alter") == "1"

    def test_natural_note_has_no_alter_element(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480, step="C", alter=0)])])
        root = _parse(render_musicxml(score))
        assert root.find(".//note/pitch/alter") is None

    def test_deleted_note_is_excluded(self) -> None:
        score = _score(
            [
                _piano_part(
                    [
                        _note(1, 0, 480, status="active"),
                        _note(2, 480, 480, status="deleted"),
                    ]
                )
            ]
        )
        root = _parse(render_musicxml(score))
        note_ids = {n.get("id") for n in root.findall(".//note")}
        assert "n1" in note_ids
        assert "n2" not in note_ids


class TestRenderMusicxmlPreconditions:
    def test_missing_onset_tick_raises_export_error(self) -> None:
        note = _note(1, 0, 480)
        note["onset_tick"] = None
        score = _score([_piano_part([note])])
        with pytest.raises(ExportError):
            render_musicxml(score)

    def test_missing_spelling_raises_export_error(self) -> None:
        note = _note(1, 0, 480)
        note["spelling"] = None
        score = _score([_piano_part([note])])
        with pytest.raises(ExportError):
            render_musicxml(score)

    def test_spelling_missing_required_key_raises_export_error_not_key_error(
        self,
    ) -> None:
        """回帰(#27-M2レビュー2巡目): spelling自体はdictでも`step`/`octave`

        キーが欠けている不正な入力は、KeyError(呼び出し元で500になる)ではなく
        ExportError(呼び出し元で422に変換される契約)として弾く。
        """
        note = _note(1, 0, 480)
        note["spelling"] = {"alter": 0}  # step/octaveが無い不正な形
        score = _score([_piano_part([note])])
        with pytest.raises(ExportError):
            render_musicxml(score)

    def test_missing_pedal_ticks_raises_export_error(self) -> None:
        score = _score(
            [
                _piano_part(
                    [_note(1, 0, 480)], pedals=[{"start_sec": 0.0, "stop_sec": 1.0}]
                )
            ]
        )
        with pytest.raises(ExportError):
            render_musicxml(score)


class TestRenderMusicxmlTiesAndMeasures:
    def test_note_crossing_a_bar_boundary_is_split_and_tied(self) -> None:
        """回帰(#27): 小節境界(4/4=1920tick)をまたぐノートは、partituraの

        `tie_notes()`によりタイで結ばれた2つのノートへ自動分割される。
        """
        score = _score([_piano_part([_note(1, 1440, 960)])])  # 1440->2400、1920をまたぐ
        root = _parse(render_musicxml(score))
        measures = root.findall(".//measure")
        assert len(measures) == 2
        ties = root.findall(".//note/tie")
        assert {t.get("type") for t in ties} == {"start", "stop"}

    def test_time_signature_change_produces_correct_measure_lengths(self) -> None:
        score = _score(
            [_piano_part([_note(1, 0, 480), _note(2, 1920, 480)])],
            time_signatures=[
                {"bar": 1, "numerator": 4, "denominator": 4},
                {"bar": 2, "numerator": 3, "denominator": 4},
            ],
        )
        root = _parse(render_musicxml(score))
        time_elements = root.findall(".//time")
        beats = [t.findtext("beats") for t in time_elements]
        assert beats == ["4", "3"]


class TestRenderMusicxmlKeySignatureAndTempo:
    def test_key_signature_fifths_is_emitted(self) -> None:
        score = _score(
            [_piano_part([_note(1, 0, 480)])],
            key_signatures=[{"bar": 1, "fifths": 2, "mode": "major"}],
        )
        root = _parse(render_musicxml(score))
        assert root.findtext(".//key/fifths") == "2"
        assert root.findtext(".//key/mode") == "major"

    def test_tempo_in_quarter_notes_is_converted_from_pulse_bpm(self) -> None:
        """回帰(#27): tempo_mapのbpmは拍追跡の拍単位(分母の音符)基準であり、

        6/8のような分母4以外の拍子では四分音符あたりのbpmへ変換が必要
        (#19で確立した「拍」の意味論)。分母8・pulse_bpm=90なら、
        1拍=8分音符=四分音符0.5個分のため、四分音符bpmは90*0.5=45。
        """
        score = _score(
            [_piano_part([_note(1, 0, 240)])],
            time_signatures=[{"bar": 1, "numerator": 6, "denominator": 8}],
            tempo_map=[{"bar": 1, "beat": 1.0, "bpm": 90.0}],
        )
        root = _parse(render_musicxml(score))
        sound = root.find(".//sound[@tempo]")
        assert sound is not None
        tempo_attr = sound.get("tempo")
        assert tempo_attr is not None
        assert float(tempo_attr) == pytest.approx(45.0)


class TestRenderMusicxmlPedal:
    def test_pedal_start_and_stop_are_emitted(self) -> None:
        score = _score(
            [
                _piano_part(
                    [_note(1, 0, 1920)],
                    pedals=[
                        {
                            "start_sec": 0.0,
                            "stop_sec": 1.0,
                            "start_tick": 0,
                            "stop_tick": 1920,
                        }
                    ],
                )
            ]
        )
        root = _parse(render_musicxml(score))
        pedals = root.findall(".//direction-type/pedal")
        assert {p.get("type") for p in pedals} == {"start", "end"}


class TestRenderMidi:
    def test_produces_a_valid_midi_file(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480), _note(2, 480, 480)])])
        midi_bytes = render_midi(score)
        assert midi_bytes[:4] == b"MThd"  # SMFヘッダチャンクの識別子

    def test_missing_precondition_raises_export_error(self) -> None:
        note = _note(1, 0, 480)
        note["spelling"] = None
        score = _score([_piano_part([note])])
        with pytest.raises(ExportError):
            render_midi(score)

    def test_midi_program_is_written_as_a_program_change(self) -> None:
        """回帰(#27-M2レビュー2巡目): partitura.save_score_midiはPart経由の

        書き出し経路ではMIDIプログラムを反映しない(常に既定の0で書き出す)ため、
        `_apply_midi_programs`が明示的にprogram changeを挿入する必要がある。
        """
        import io

        import mido

        part = _piano_part([_note(1, 0, 480)])
        part["midi_program"] = 40  # バイオリン(0始まり)
        score = _score([part])
        midi_bytes = render_midi(score)

        midi_file = mido.MidiFile(file=io.BytesIO(midi_bytes))
        program_changes = [
            msg
            for track in midi_file.tracks
            for msg in track
            if msg.type == "program_change"
        ]
        assert len(program_changes) == 1
        assert program_changes[0].program == 40


class TestRenderMusicxmlReparses:
    def test_output_reparses_with_music21(self) -> None:
        """生成したMusicXMLが、独立した別ライブラリ(music21)で妥当にパースできる

        (partitura自身の再パースだけでは「自分で作った歪みも自分で読める」
        だけになりかねないため、外部検証として意味がある)。
        """
        import music21

        score = _score(
            [
                _piano_part(
                    [
                        _note(1, 0, 480, staff=1),
                        _note(2, 0, 960, staff=2, midi=48, step="C", octave=3),
                    ]
                )
            ]
        )
        xml_bytes = render_musicxml(score)
        parsed = music21.converter.parseData(
            xml_bytes.decode("utf-8"), format="musicxml"
        )
        assert len(parsed.flatten().notes) >= 2
