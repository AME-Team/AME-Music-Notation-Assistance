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

from app.pipeline.export.score_builder import ExportError, build_score

__all__ = ["ExportError", "render_musicxml"]


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


def render_musicxml(score: dict[str, Any]) -> bytes:
    """Score IRのJSON dict形状からMusicXMLバイト列を生成する(#27)。

    量子化(#25)+L0(#26)が実行済み(全ノートに`onset_tick`/`duration_tick`、
    有効な`spelling`が設定済み)であることが前提。未実行の場合は
    `ExportError`を送出する(API層で422相当に変換すること)。
    """
    import partitura

    partitura_score = build_score(score)
    xml_bytes = partitura.save_musicxml(partitura_score, out=None)
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")
    xml_str = _inject_score_instruments(xml_bytes.decode("utf-8"), score.get("parts", []))
    return xml_str.encode("utf-8")
