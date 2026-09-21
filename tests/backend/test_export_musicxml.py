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
from app.pipeline.export.musicxml import (
    ExportError,
    _count_sounding_notes,
    _ensure_notes_written,
    _expected_note_count,
    render_musicxml,
)
from app.pipeline.export.musicxml import (
    _inject_score_instruments as inject_score_instruments,
)
from app.pipeline.export.musicxml import (
    _pad_parts_to_equal_measures as pad_parts_to_equal_measures,
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


class TestEmptyMusicxmlDetection:
    """#137: ノート0件のMusicXMLを無言で正常終了させない。

    #60の実曲検証で、`ScoreIR`の拍子が空だとpartituraが小節を1つも生成できず、
    ノート0件(1,599バイト)のMusicXMLを正常終了で返していた(原因は#136で修正済み)。
    ここでは書き出し後の入出力突き合わせが機能することを固定する。
    """

    def test_zero_note_output_raises_when_score_has_notes(self) -> None:
        """ノートがあるのに出力0件なら ExportError(#136の症状そのもの)。

        実データでも `time_signatures=[]` のスコア(4パート・568ノート)に対して
        この検出が働くことを確認済み。
        """
        score = _score([_piano_part([_note(1, 0, 480)])])
        xml = "<score-partwise><part-list/></score-partwise>"
        with pytest.raises(ExportError, match="generated MusicXML has no notes"):
            _ensure_notes_written(score, xml)

    def test_more_output_than_input_is_allowed(self) -> None:
        """タイ分割で出力が入力より多い場合(実データ568→647)はエラーにしない。"""
        score = _score([_piano_part([_note(1, 0, 480)])])
        xml = "<note><pitch><step>C</step></pitch></note><note><pitch/></note>"
        _ensure_notes_written(score, xml)  # 例外が出ないこと

    def test_empty_output_is_allowed_when_score_has_no_notes(self) -> None:
        score = _score([_piano_part([])])
        _ensure_notes_written(score, "<score-partwise><part-list/></score-partwise>")

    def test_normal_score_is_not_rejected(self) -> None:
        score = _score([_piano_part([_note(1, 0, 480), _note(2, 480, 480)])])
        xml = render_musicxml(score)
        assert _count_sounding_notes(xml.decode("utf-8")) == 2

    def test_deleted_notes_are_excluded_from_the_expected_count(self) -> None:
        score = _score(
            [_piano_part([_note(1, 0, 480), _note(2, 480, 480, status="deleted")])]
        )
        assert _expected_note_count(score) == 1
        xml = render_musicxml(score)
        assert _count_sounding_notes(xml.decode("utf-8")) == 1

    def test_rests_are_not_counted_as_sounding_notes(self) -> None:
        xml = (
            "<measure><note><rest/><duration>480</duration></note>"
            "<note><pitch><step>C</step><octave>4</octave></pitch></note></measure>"
        )
        assert _count_sounding_notes(xml) == 1

    def test_tie_splits_are_not_treated_as_lost_notes(self) -> None:
        """小節線をまたぐノートはタイ分割されるため、出力要素数は入力より多くなりうる

        (実データで568→647)。その場合もエラーにしないことを固定する。
        """
        score = _score([_piano_part([_note(1, 0, 1920)])])  # 4拍(1小節)ぴったり
        xml = render_musicxml(score)
        assert _count_sounding_notes(xml.decode("utf-8")) >= 1


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

    def test_tempo_map_is_embedded_as_set_tempo(self) -> None:
        """#36: tempo_mapが実際にSMFの`set_tempo`メタメッセージとして

        埋め込まれることを確認する(DAWでの正しい再生速度に必要)。
        `build_score`(score_builder.py)はMusicXML/MIDI共有のため
        `Tempo`オブジェクトの追加自体は既存だが、MIDI側でこれが実際に
        `set_tempo`として出力されることを検証する専用テストはこれまで
        無かった。
        """
        import io

        import mido

        score = _score(
            [_piano_part([_note(1, 0, 480)])],
            tempo_map=[{"bar": 1, "beat": 1.0, "bpm": 140.0}],
        )
        midi_bytes = render_midi(score)

        midi_file = mido.MidiFile(file=io.BytesIO(midi_bytes))
        tempo_messages = [
            msg
            for track in midi_file.tracks
            for msg in track
            if msg.type == "set_tempo"
        ]
        assert len(tempo_messages) == 1
        # MIDIのテンポは「1拍あたりのマイクロ秒」を整数で表すため、bpmとの
        # 相互変換に浮動小数点の丸め誤差が乗る(実測: 140.00014000014)。
        # 既定の相対許容誤差(1e-6)では失敗するため、実用上十分な精度
        # (0.01bpm)で比較する。
        assert mido.tempo2bpm(tempo_messages[0].tempo) == pytest.approx(140.0, abs=0.01)


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


class TestPartsHaveEqualMeasureCounts:
    """#155: 全パートの小節数を揃える。

    MusicXMLは全パートが同じ小節数を持つ前提で小節番号をパート間に対応させる。
    partituraの`add_measures`は各パートの最終イベントまでしか小節を作らないため、
    「長く鳴るパート」と「早く終わるパート」が混在すると小節数がずれ、その状態の
    MusicXMLはOSMDが読み込めず`ScorePreview`が例外で落ちる(#60の実曲検証で確認:
    piano=23小節 / 他パート=17小節)。
    """

    @staticmethod
    def _part(part_id: str, notes: list[dict]) -> dict:
        return {
            "id": part_id,
            "name": part_id.title(),
            "midi_program": 0,
            "staves": 1,
            "clefs": [{"staff": 1, "sign": "G", "line": 2}],
            "notes": notes,
            "pedals": [],
        }

    @staticmethod
    def _measure_counts(xml: bytes) -> dict[str, int]:
        root = ET.fromstring(xml)
        return {
            str(part.get("id")): len(part.findall("measure"))
            for part in root.findall("part")
        }

    def test_short_part_is_padded_to_the_longest_part(self) -> None:
        long_notes = [_note(i, i * 480, 480) for i in range(8)]  # 2小節分
        short_notes = [_note(100, 0, 480)]  # 1小節分
        score = _score(
            [self._part("piano", long_notes), self._part("bass", short_notes)]
        )

        counts = self._measure_counts(render_musicxml(score))

        assert counts["piano"] == 2, counts
        assert counts["bass"] == counts["piano"], counts

    def test_padded_measures_are_appended_with_sequential_numbers(self) -> None:
        long_notes = [_note(i, i * 480, 480) for i in range(8)]
        short_notes = [_note(100, 0, 480)]
        score = _score(
            [self._part("piano", long_notes), self._part("bass", short_notes)]
        )

        root = ET.fromstring(render_musicxml(score))
        bass = next(part for part in root.findall("part") if part.get("id") == "bass")

        assert [m.get("number") for m in bass.findall("measure")] == ["1", "2"]

    def test_notes_of_the_short_part_stay_in_its_own_measures(self) -> None:
        """補った空小節に元の音符が混ざらない(詰め直しではなく末尾への追記である)。"""
        long_notes = [_note(i, i * 480, 480) for i in range(8)]
        short_notes = [_note(100, 0, 480)]
        score = _score(
            [self._part("piano", long_notes), self._part("bass", short_notes)]
        )

        root = ET.fromstring(render_musicxml(score))
        bass = next(part for part in root.findall("part") if part.get("id") == "bass")
        notes_per_measure = [len(m.findall("note")) for m in bass.findall("measure")]

        assert notes_per_measure[0] == 1, notes_per_measure
        assert notes_per_measure[1:] == [0] * (len(notes_per_measure) - 1), (
            notes_per_measure
        )

    def test_aligned_parts_are_left_untouched(self) -> None:
        notes = [_note(i, i * 480, 480) for i in range(8)]
        score = _score(
            [
                self._part("piano", notes),
                self._part("bass", [_note(100, i * 480, 480) for i in range(8)]),
            ]
        )

        counts = self._measure_counts(render_musicxml(score))

        assert counts == {"piano": 2, "bass": 2}, counts

    def test_single_part_score_is_unchanged(self) -> None:
        notes = [_note(i, i * 480, 480) for i in range(4)]
        score = _score([self._part("piano", notes)])

        counts = self._measure_counts(render_musicxml(score))

        assert counts == {"piano": 1}, counts

    def test_helper_is_a_no_op_when_counts_match(self) -> None:
        xml = '<score-partwise><part id="p"><measure number="1"></measure></part><part id="q"><measure number="1"></measure></part></score-partwise>'

        assert pad_parts_to_equal_measures(xml) == xml

    def test_helper_rejects_xml_without_part_elements(self) -> None:
        with pytest.raises(ExportError):
            pad_parts_to_equal_measures("<score-partwise></score-partwise>")
