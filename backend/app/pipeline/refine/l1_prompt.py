"""L1: システムプロンプト+記譜ルール集の構築(#38, 設計書§7.3/§8.6)。

**実際のAnthropic API呼び出しは一切行わない**(#39の担当)。ここは静的な
プロンプト文字列を組み立てるだけの、外部AI呼び出しへの依存ゼロのモジュール。

`NOTATION_RULES_MARKDOWN`はM5(#8.6)のエージェントワークスペース
(`agent_workspace/{run_id}/notation_rules.md`)でも同一内容を使う設計のため、
この定数を単一の情報源とする(重複すると記譜方針がL1とL2で食い違うリスクが
あるため)。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.pipeline.refine.key_estimation import estimate_key
from app.pipeline.refine.l1_chunker import ChunkInput
from app.services.score_service import ScoreService

if TYPE_CHECKING:
    from app.domain.score import ScoreIR

NOTATION_RULES_MARKDOWN = """\
# 記譜ルール集

## 異名同音の選択
- 調号に従うことを基本とする。シャープ系の調はシャープ表記、フラット系の調はフラット表記を優先する。
- 直前の音からの半音進行(上行/下行)がある場合は、進行方向に自然な表記を優先する
  (上行ならシャープ側、下行ならフラット側)。
- 機能和声上、借用和音や導音として意味を持つ表記がある場合はそちらを優先してよい
  (例: 属七の第3音は導音として半音上の表記が自然なことが多い)。

## 声部・大譜表の割り当て
- ピアノは音高が中央ハ(MIDI 60)以上ならト音部(staff 1)、未満ならヘ音部(staff 2)を基本とする。
- 同一staff内で同時発音がある場合、高い方から voice 1, 2, 3, 4 の順に割り当てる。
  同時発音するノートを全て同じvoiceへまとめてはならない(同一voice内で時間的に重なる
  ノートは記譜として不正。検証層V-8が違反として検出し、voiceのみ決定論的に再割当する
  自動修復を試みる。ただし修復は4声に収まる範囲でのみ可能で、修復できない場合や他の
  違反が併存する場合はチャンク全体が棄却され、voice以外の判断も失われる)。
- 開始タイミングが異なる場合も同じで、先行する音が鳴っている途中で新しい音が始まるなら
  別のvoiceへ回す(1つのvoiceは常に単旋律として扱う。サステイン・アルペジオ・ペダルに
  よる余韻で頻出する)。
- 旋律的なまとまり(フレーズ)は同一voiceに揃え、跳躍のたびにvoiceを切り替えない。

## タイ・休符のグルーピング
- 小節線をまたぐノートは、小節線上でタイに分割する。
- 標準的な音価(全音符・付点二分音符等)に収まらない長さのノートは、
  自然な音価の組み合わせにタイで分割する。
- 休符は可能な限り少ない数でグルーピングする(例: 4分休符4つではなく全休符1つ)。

## Doricoでの取り込みを前提とした制約
- 声部数はパートあたり最大4声部までとする(Doricoの標準的な譜表レイアウトに合わせる)。
- タイの分割点は必ず既存ノートの発音区間内に収めること(区間外への分割は不正な記譜になる)。
"""

_ROLE_AND_OUTPUT_CONTRACT = """\
あなたは音楽記譜の専門家です。AMT(自動採譜)と決定論的クオンタイザの出力に対して、
文脈がないと決められない記譜判断(異名同音の選択・声部/大譜表の割り当て・
スナップ候補からの選択・ゴーストノートの最終判定・タイ分割)のみを行います。

タイミング数値そのもの(オンセット時刻・音価)を新たに生成することはありません。
必ず入力で提示された`snap_candidates`のいずれかを選んでください。
出力は指定されたJSON Schemaに厳密に従ってください。
"""

_DORICO_CONSTRAINTS = """\
出力はDorico(記譜ソフトウェア)への取り込みを前提とします。
記譜ルール集(異名同音・声部・タイ・休符グルーピング)に従い、演奏者が読みやすい
記譜を優先してください。
"""

# #109: 記譜ルール集の中に埋もれていた「同時発音のvoice分割」規則の遵守率が
# 実測(#67/#68)で低かった(同時発音するコード構成音を全てvoice=1へ一括りにする
# パターンが頻発し、V-8違反→チャンク全体棄却を招いていた)ため、**出力直前**に
# 具体例つきで再掲する。システムプロンプトの可変情報禁止(§7.3)は維持し、
# 常に同一バイト列を返す(最後に置くことで、モデルが直前に読む位置に規則が来る)。
_VOICE_ASSIGNMENT_CHECK = """\
## 出力前の最終確認(voice割り当て)
- 同一staff・同一タイミング(同一onset)で2音以上が同時に鳴る場合、それらを**同じvoiceに
  まとめてはならない**。高い音から順に voice 1, 2, 3, 4 を割り当てること。
- 開始タイミングが異なる音でも、先行する音が鳴っている途中で新しい音が始まる場合は
  別のvoiceへ回すこと(サステイン・アルペジオ・ペダルの余韻で頻出する)。
- 同一voice内で時間的に重なるノートがあると、検証層(V-8: 同一voice内の時間重複)が違反
  として検出し、**そのvoiceだけを決定論的に再割当して自動修復する**。ただし修復は4声に
  収まる範囲でのみ可能であり、修復できない場合や他の違反が併存する場合は
  **チャンク全体の提案が採用されない**(voice以外の判断もすべて失われる)。
  自動修復に頼らず、最初から規則どおりに割り当てること。

例: staff 1の中で C4 / E4 / G4 が同時に鳴る場合、最低音C4が voice 3、中音E4が voice 2、
最高音G4が voice 1(同一staff内でvoice番号1〜4を重複させない)。
"""


def build_system_prompt() -> str:
    """役割定義+出力契約+記譜ルール集+Dorico向け制約+voice割り当ての最終確認。

    キャッシュ効率のため(§7.3「キャッシュ無効化の禁止事項」)、タイムスタンプ・
    project_id・チャンク番号等の可変情報は一切含めない。曲をまたいで再利用される
    システムプロンプトとして、常に同一のバイト列を返す(既定引数もグローバル定数も
    無いため呼び出すたびに同じ文字列になる)。

    `_VOICE_ASSIGNMENT_CHECK`(#109)を**最後**に置くのは、実測(#67/#68)で
    同時発音のvoice分割規則の遵守率が低かったため、規則と具体例をモデルが
    最も直前に読む位置へ移す意図(§7.3プロンプト構造1〜3番の範囲内)。
    """
    return "\n".join(
        [
            _ROLE_AND_OUTPUT_CONTRACT,
            NOTATION_RULES_MARKDOWN,
            _DORICO_CONSTRAINTS,
            _VOICE_ASSIGNMENT_CHECK,
        ]
    )


def build_song_context_message(score: ScoreIR, part_id: str) -> str:
    """楽曲全体コンテキスト(調・拍子・テンポ・楽器編成、§7.3プロンプト構造4番)。

    曲単位でキャッシュされる想定(チャンクをまたいで不変)のため、小節範囲や
    個別ノートの情報は含めない。
    """
    part = score.find_part(part_id)
    if part is None:
        raise ValueError(f"part {part_id!r} not found in score")

    time_signatures = [
        f"{ts.numerator}/{ts.denominator}(小節{ts.bar}〜)" for ts in score.time_signatures
    ] or ["4/4(既定)"]
    tempos = [f"{t.bpm}bpm(小節{t.bar}〜)" for t in score.tempo_map] or ["120bpm(既定)"]
    # パート全体のノートから推定した調(#38 Gate2レビュー指摘: 以前はdocstringが
    # 「調」を含むと謳いながら実装に無かった)。`l1_chunker.build_chunks`の
    # チャンクごとのkey_estimateは対象小節のノートのみから局所的に推定するため、
    # チャンク間で調が食い違いうるが、ここ(曲単位でキャッシュされる文脈)では
    # パート全体を1つの安定した調としてまとめて提示する。
    pitch_classes = [note.midi % 12 for note in part.notes if note.status != "deleted"]
    key = estimate_key(pitch_classes)

    lines = [
        "## 楽曲全体コンテキスト",
        f"- パート: {part.name}({part.id}), {part.staves}段譜表",
        f"- 調: {key.label}(信頼度{key.confidence:.2f})",
        f"- 拍子: {', '.join(time_signatures)}",
        f"- テンポ: {', '.join(tempos)}",
    ]
    chords = ScoreService.ensure_chords(score)
    if chords:
        chords_summary = ", ".join(f"m.{c.bar}:{c.symbol}" for c in chords[:8])
        if len(chords) > 8:
            chords_summary += " ..."
        lines.append(f"- コード進行: {chords_summary}")

    return "\n".join(lines)


def build_chunk_message(chunk: ChunkInput) -> str:
    """チャンク入力JSON(§7.3プロンプト構造5番、毎回変わる部分)。

    設計書§7.3の指示通り、JSONシリアライズは常にキー順ソートする
    (`sort_keys=True`)。ソートしないとチャンクごとにキー順が変わりうる
    (Pythonのdict挿入順依存)ため、プロンプトキャッシュのヒット率に影響しうる
    (完全一致でなければキャッシュされないシステムプロンプト部分ほど致命的
    ではないが、一貫性のため同じ方針を適用する)。
    """
    return json.dumps(chunk.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
