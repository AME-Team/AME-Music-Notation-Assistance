"""Stage 6: MusicXML書き出し(#27, §6 Stage 6)。

`score_builder.build_score`(Score IR → partitura `Score`)を使い、
`save_musicxml`でMusicXMLバイト列を生成する。partituraの挙動や既知の制約は
`score_builder.py`のモジュールdocstringを参照。

`pipeline/` はdomain非依存の方針(`beat.py`/`quantize.py`と同様)のため、
入力はdomainモデルではなく Score IR と同じJSON形状の生dict
(`ScoreIR.model_dump(mode="json")`)で受け取る。呼び出し元(`api/export.py`)
がdomainモデルとの変換を担う。
"""

from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from app.pipeline.export.score_builder import ExportError, build_score, is_exportable_note

__all__ = ["ExportError", "render_musicxml"]


_NOTE_ELEMENT_PATTERN = re.compile(r"<note\b[^>]*/>|<note\b[^>]*>.*?</note>", re.DOTALL)


def _expected_note_count(score: dict[str, Any]) -> int:
    """書き出し対象のノート数(`score_builder.is_exportable_note`と同一基準)。"""
    return sum(
        1
        for part_data in score.get("parts", [])
        for note in part_data.get("notes", [])
        if is_exportable_note(note)
    )


def _count_sounding_notes(xml_str: str) -> int:
    """生成されたMusicXML内で音を出す`<note>`要素の数(休符は数えない)。"""
    return sum(1 for block in _NOTE_ELEMENT_PATTERN.findall(xml_str) if "<rest" not in block)


def _inject_score_instruments(xml_str: str, parts_data: list[dict]) -> str:
    """`<score-part>`ごとに`<score-instrument>`/`<midi-instrument>`を追加する(#27)。

    partituraの`save_musicxml`はこれらを出力しないため、文字列レベルの
    ピンポイント挿入で補う(#27-M2レビュー指摘: `xml.etree`でのパース→
    再シリアライズは`<!DOCTYPE ...>`宣言やpartituraが挿入する小節区切り
    コメントを失う副作用があるため避ける)。

    置換関数を`re.subn`へ渡す(#27-M2レビュー2巡目の指摘): 置換文字列を
    直接渡すと、パート名に`\\1`等のバックスラッシュシーケンスが含まれた
    場合に後方参照として誤解釈され、出力が破壊されたり`re.error`になる。

    マッチ件数を検証する(#27-M2レビュー3巡目の指摘): `_count`を捨てて
    いると、partituraの出力フォーマットが変わり`<score-part id="...">`の
    パターンに一致しなくなった場合、`<score-instrument>`が無言で欠落した
    MusicXMLを返してしまう。`midi.py`が`strict=True`で前提崩れを検出する
    方針と揃え、ここでも`ExportError`で明示的に検出する。
    """
    for part_data in parts_data:
        part_id = part_data["id"]
        instrument_id = f"{part_id}-I1"
        midi_program = int(part_data.get("midi_program", 0)) + 1  # MusicXMLは1始まり
        name = _xml_escape(part_data["name"])
        addition = (
            f'<score-instrument id="{instrument_id}">'
            f"<instrument-name>{name}</instrument-name>"
            f"</score-instrument>"
            f'<midi-instrument id="{instrument_id}">'
            f"<midi-program>{midi_program}</midi-program>"
            f"</midi-instrument>"
        )
        pattern = re.compile(
            rf'(<score-part id="{re.escape(part_id)}">.*?)(</score-part>)', re.DOTALL
        )

        def _insert_addition(m: re.Match[str], addition: str = addition) -> str:
            return m.group(1) + addition + m.group(2)

        xml_str, count = pattern.subn(_insert_addition, xml_str, count=1)
        if count != 1:
            raise ExportError(
                f"could not locate <score-part id={part_id!r}> in the generated MusicXML "
                "to inject score-instrument/midi-instrument"
            )
    return xml_str


def _ensure_notes_written(score: dict[str, Any], xml_str: str) -> None:
    """書き出したMusicXMLにノートが入っていることを確認する(#137)。

    #60の実曲検証で、`ScoreIR`の拍子が空だとpartituraが小節を1つも生成できず、
    **ノート0件(1,599バイト)のMusicXMLを正常終了で返す**ことが判明した(原因自体は
    #136で修正済み)。生成側の前提が将来崩れても「空のファイルを成功として返す」状態に
    戻らないよう、ここで明示的に検出する(`midi.py`が`strict=True`でトラック数の
    前提崩れを検出する方針と揃える)。

    要素数の等値比較・減少の警告はしない: partituraは小節線をまたぐノートをタイ分割
    するため出力は入力より**多くなりうる**し(実データで568→647)、逆に入力のタイが
    `tie_notes`で統合されて正当に減ることもある。つまり要素数だけでは「欠落」と
    区別できないため、警告を出すと正当なスコアでも毎回ログが出てノイズになる
    (#137レビュー指摘)。検知するのは「ノートが1件も出ていない」場合に限る。
    """
    expected_notes = _expected_note_count(score)
    written_notes = _count_sounding_notes(xml_str)
    if expected_notes > 0 and written_notes == 0:
        raise ExportError(
            f"generated MusicXML has no notes although the score has {expected_notes} notes; "
            "the score could not be laid out into measures (check time_signatures/tempo_map/"
            "divisions in the Score IR)"
        )


def render_musicxml(score: dict[str, Any]) -> bytes:
    """Score IRのJSON dict形状からMusicXMLバイト列を生成する(#27)。

    量子化(#25)+L0(#26)が実行済み(全ノートに`onset_tick`/`duration_tick`、
    有効な`spelling`が設定済み)であることが前提。未実行の場合は
    `ExportError`を送出する(API層で422相当に変換すること)。

    書き出し後に入力ノート数と`<note>`要素数を突き合わせ、**ノート0件のまま正常終了
    しない**ことを保証する(#137: #60の実曲検証で、`ScoreIR`の拍子が空だとpartituraが
    小節を生成できず、ノート0件のMusicXMLを無言で返していた。原因は#136で修正済み)。
    なおpartituraは小節線をまたぐノートをタイ分割するため、**出力の要素数は入力の
    ノート数より多くなりうる**(実データで568→647)ので、等値比較はしない。
    """
    import partitura

    partitura_score = build_score(score)
    xml_bytes = partitura.save_musicxml(partitura_score, out=None)
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")
    xml_str = _inject_score_instruments(xml_bytes.decode("utf-8"), score.get("parts", []))

    # #137: 入出力のノート数を突き合わせ、書き出しの黙った失敗を検出する。
    # #60の実曲検証で、`ScoreIR`の拍子が空だとpartituraが**小節を1つも生成せず、
    # ノート0件(1,599バイト)のMusicXMLを正常終了で返す**ことが判明した(原因自体は
    # #136で修正済み)。生成側の前提が将来崩れても「空のファイルを成功として返す」
    # 状態に戻らないよう、ここで明示的に検出する(midi.pyが`strict=True`でトラック数の
    # 前提崩れを検出する方針と揃える)。
    _ensure_notes_written(score, xml_str)
    return xml_str.encode("utf-8")
