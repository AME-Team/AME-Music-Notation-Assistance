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

from app.pipeline.export.score_builder import (
    DEFAULT_DIVISIONS,
    ExportError,
    build_score,
    is_exportable_note,
)
from app.pipeline.time_signature import time_signature_at_bar

__all__ = ["ExportError", "render_musicxml"]


_NOTE_ELEMENT_PATTERN = re.compile(r"<note\b[^>]*/>|<note\b[^>]*>.*?</note>", re.DOTALL)
_PART_ELEMENT_PATTERN = re.compile(r"(<part(?:\s[^>]*)?>)(.*?)(</part>)", re.DOTALL)
_MEASURE_OPEN_PATTERN = re.compile(r"<measure\b[^>]*>")
_MEASURE_NUMBER_PATTERN = re.compile(r"<measure\b[^>]*\bnumber=\"([^\"]*)\"", re.DOTALL)
_PEDAL_END_TYPE_PATTERN = re.compile(r'(<pedal\b[^>]*\btype=")end(")')


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


def _fix_pedal_stop_type(xml_str: str) -> str:
    """partituraが出力するペダル終端の`type="end"`を`type="stop"`へ補正する(#162)。

    MusicXMLスキーマの`pedal-type`列挙値は`start`/`stop`/`sostenuto`/`change`/
    `continue`/`discontinue`/`resume`のみで`end`は含まれない。partitura自身の
    バグ(`exportmusicxml.py`がペダル終端要素に`type="end"`をハードコード)で、
    Doricoは実機で検証エラーとしてこれを拒否する(実測)。
    """
    return _PEDAL_END_TYPE_PATTERN.sub(r"\1stop\2", xml_str)


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


def _measure_lengths_in_divisions(score: dict[str, Any], bar_count: int) -> dict[int, int]:
    """小節番号(1始まり) → その小節の長さ(divisions単位) を返す(#154レビュー指摘)。

    補完する小節に全休符を入れて小節長を明示するために使う。長さは
    `ScoreIR`の拍子から `分子 * divisions * 4 / 分母` で求める
    (`time_signature_at_bar`は拍子が未定義の小節を直前の拍子で補う)。
    """
    divisions = score.get("divisions", DEFAULT_DIVISIONS)
    time_signatures = score.get("time_signatures", [])
    lengths: dict[int, int] = {}
    for bar in range(1, bar_count + 1):
        numerator, denominator = time_signature_at_bar(time_signatures, bar)
        lengths[bar] = int(numerator * divisions * 4 / denominator)
    return lengths


def _next_measure_number(body: str, count: int) -> int:
    """そのパートで次に付ける小節番号を決める(#154レビュー指摘)。

    小節番号は弱起(`number="0"`始まり)や繰り返しで連番にならないことがある
    (MusicXMLでは小節は**位置**で対応し、番号は目安)。そのため小節数から
    番号を推測せず、そのパートの最後の`<measure number="...">`の続き番号を
    使う。番号が無い/数値でない場合は小節数の連番(1始まり)へフォールバックする。
    """
    numbers = _MEASURE_NUMBER_PATTERN.findall(body)
    if numbers:
        try:
            return int(numbers[-1]) + 1
        except ValueError:
            pass
    return count + 1


def _rest_measure_element(number: int, length_divisions: int | None) -> str:
    """補完する小節の要素を組み立てる。

    長さが分かる場合は**全休符**(`<rest measure="yes"/>`)を入れて小節長を明示する
    (#154レビュー指摘: 音符も`<forward>`も無い空の小節は長さが未定義で、
    拍子から補われない実装だとレイアウトが崩れうる)。MusicXMLの規約どおり
    `measure="yes"`の休符は`<type>whole</type>`とし、`<duration>`へ実際の
    小節長を入れる。休符の段・声部が不定にならないよう`<voice>`/`<staff>`も明示する
    (全休符は慣例どおり最上段=staff 1に置く)。長さ不明の場合は従来どおり空の小節にする。
    """
    if length_divisions is None:
        return f'<measure number="{number}"></measure>'
    rest_note = (
        f'<note><rest measure="yes"/><duration>{length_divisions}</duration>'
        f"<voice>1</voice><type>whole</type><staff>1</staff></note>"
    )
    return f'<measure number="{number}">{rest_note}</measure>'


def _pad_parts_to_equal_measures(xml_str: str, score: dict[str, Any] | None = None) -> str:
    """全パートの小節数を最大値に揃え、足りないパートへ補完小節を追記する(#154)。

    MusicXML(`score-partwise`)は**全パートが同じ小節数を持つ**ことを前提にしており、
    小節番号がパート間で対応しているものとして扱われる。ところがpartituraの
    `add_measures`は各パートの最終イベント(`part.last_point.t`)までしか小節を作らないため、
    「長く鳴るパート」と「早く終わるパート」が混在すると小節数がずれる
    (#60の実曲検証: piano=23小節 / bass・vocals・guitar・other=17小節)。

    この状態のMusicXMLは譜面表示ライブラリ(OSMD)が読み込めず、
    `<ScorePreview>`が例外で落ちる(実測: `Cannot read properties of undefined
    (reading 'staffEntries')` / `(reading 'parent')`、#154)。パート単体では読めるのに
    複数パートにすると落ちるのがこの不整合の特徴。

    `score`を渡すと、補完する小節へ**全休符と小節長**を入れる(渡さない場合は空の
    小節)。既存の`xml.etree`を使わない文字列レベル処理の方針は
    `_inject_score_instruments`と同じ(パース→再シリアライズで`<!DOCTYPE ...>`宣言や
    partituraが挿入する小節区切りコメントが失われるのを避ける)。partituraの出力は
    `<measure number="n">`〜`</measure>`形式なので同じ形式で追記する。
    """
    matches = _PART_ELEMENT_PATTERN.findall(xml_str)
    if not matches:
        raise ExportError("generated MusicXML has no <part> element")
    measure_counts = [len(_MEASURE_OPEN_PATTERN.findall(body)) for _, body, _ in matches]
    target = max(measure_counts)
    if min(measure_counts) == target:
        return xml_str

    lengths = _measure_lengths_in_divisions(score, target) if score is not None else {}

    def _pad(match: re.Match[str]) -> str:
        body = match.group(2)
        count = len(_MEASURE_OPEN_PATTERN.findall(body))
        if count >= target:
            return match.group(0)
        first = _next_measure_number(body, count)
        # 長さは**小節番号**ではなく**位置**(1始まり)で引く(#154レビュー指摘):
        # 小節番号は弱起(`number="0"`始まり)等で位置と一致しない。
        extra = "".join(
            _rest_measure_element(first + offset, lengths.get(count + offset + 1))
            for offset in range(target - count)
        )
        return match.group(1) + body + extra + match.group(3)

    return _PART_ELEMENT_PATTERN.sub(_pad, xml_str)


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
    # #162: partituraが出力するペダル終端の`type="end"`はMusicXMLスキーマ違反
    # (Doricoが検証エラーで拒否する)。`type="stop"`へ補正する。
    xml_str = _fix_pedal_stop_type(xml_str)

    # #137: 入出力のノート数を突き合わせ、書き出しの黙った失敗を検出する。
    # #60の実曲検証で、`ScoreIR`の拍子が空だとpartituraが**小節を1つも生成せず、
    # ノート0件(1,599バイト)のMusicXMLを正常終了で返す**ことが判明した(原因自体は
    # #136で修正済み)。生成側の前提が将来崩れても「空のファイルを成功として返す」
    # 状態に戻らないよう、ここで明示的に検出する(midi.pyが`strict=True`でトラック数の
    # 前提崩れを検出する方針と揃える)。
    _ensure_notes_written(score, xml_str)
    # #154: 全パートの小節数を揃える(揃っていないMusicXMLは譜面表示ライブラリが読めない)。
    xml_str = _pad_parts_to_equal_measures(xml_str, score)
    return xml_str.encode("utf-8")
