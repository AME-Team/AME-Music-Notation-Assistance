"""L1: チャンク分割と文脈注入(#38, 設計書§7.3)。

Score IR(1パート)を、L1(構造化出力による注釈)への入力チャンクの列に変換する。
**実際のAnthropic API呼び出しは一切行わない**(#39の担当)。この段階は完全に
決定論的なScore IR→JSON変換であり、外部AI呼び出しへの依存ゼロでテストできる。

`pipeline/`は外部依存(domain含む)を持たない方針だが、`domain.score.ScoreIR`
自体は入力として受け取る(呼び出し元がdomainモデルを保持しているため、
`model_dump(mode="json")`で生dictへ変換してから内部処理する。`score_builder.py`
と同じパターン)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.pipeline.refine.key_estimation import estimate_key
from app.pipeline.time_signature import tick_to_bar_beat, time_signature_at_bar

if TYPE_CHECKING:
    from app.domain.score import ScoreIR

DEFAULT_BARS_PER_CHUNK = 4
SHRUNK_BARS_PER_CHUNK = 2
# 「テンポが速い/音数が多い場合は2小節に自動縮小」(設計書§7.3)の具体的な閾値は
# 設計書に明記が無いため、#38の実装判断として以下を採用する。
DENSE_NOTES_PER_BAR_THRESHOLD = 20
FAST_TEMPO_BPM_THRESHOLD = 160.0
DEFAULT_TEMPO_BPM = 120.0


class ChunkPart(BaseModel):
    id: str
    instrument: str
    staves: int


class ChordHint(BaseModel):
    bar: int
    beat: float
    symbol: str


class ChunkBars(BaseModel):
    target: tuple[int, int]
    context_before: list[int]
    context_after: list[int]


class ChunkContext(BaseModel):
    key_estimate: str
    key_confidence: float
    time_signature: str
    tempo_bpm: float
    part: ChunkPart
    chord_hints: list[ChordHint]
    bars: ChunkBars


class SnapCandidateInput(BaseModel):
    id: str
    beat: float
    duration_beat: float
    grid: str
    cost: float


class ChunkNote(BaseModel):
    id: int
    bar: int
    raw_beat: float
    raw_duration_beat: float
    midi: int
    velocity: int
    confidence: float
    flags: list[str]
    snap_candidates: list[SnapCandidateInput]
    editable: bool


class ChunkInput(BaseModel):
    context: ChunkContext
    notes: list[ChunkNote]


def _bpm_at_bar(tempo_map: list[dict], bar: int) -> float:
    """`bar`時点で有効なbpm(順方向に補完)。指定が無ければ既定120。

    `time_signature_at_bar`と同じ前方補完パターン(tempo_mapの`beat`位置は
    ここでは無視し、`bar`の粒度のみで十分とする、#38の意図的な簡略化。
    厳密な小節内拍位置まで見た補完は、この用途(LLMへの参考情報)には過剰)。
    """
    bpm = DEFAULT_TEMPO_BPM
    for entry in sorted(tempo_map, key=lambda t: (t["bar"], t["beat"])):
        if entry["bar"] <= bar:
            bpm = entry["bpm"]
        else:
            break
    return bpm


def _duration_beats(
    note: dict, *, note_bar: int, time_signatures: list[dict], divisions: int
) -> float:
    """noteの長さを拍数換算する。

    小節境界をまたいで拍子が変わるノートは、開始小節の拍子を基準に換算する
    (#38の意図的な簡略化: LLMへの参考情報であり、検証層(V-9等)が使う値では
    ないため、稀な境界ケースでの多少の誤差は許容する)。
    """
    _, denominator = time_signature_at_bar(time_signatures, note_bar)
    pulse_ticks = divisions * 4 / denominator
    return note["duration_tick"] / pulse_ticks


def _snap_candidates_for_note(
    note: dict,
    *,
    note_beat: float,
    note_bar: int,
    time_signatures: list[dict],
    divisions: int,
) -> list[SnapCandidateInput]:
    """`Note.snap_candidates`(Stage 4、#25)をL1入力の`snap_candidates`形式に変換する。

    `cost`は設計書の例(0.02〜0.34程度、小さいほど良い)に合わせ、`note_beat`
    (raw_beat、#38の簡略化により実質「自動選択された最良候補の位置」)との
    beat単位での距離とする。バックエンド内部の`SnapCandidate.score`は
    ランキング用の任意スケール値(強拍重み込みで大きいほど良い、0〜1に
    正規化されていない)であり、design docの`cost`とスケールが異なるため
    そのまま変換すると意味を成さない(#38実装時に判明、変換せず独自定義する)。

    `duration_beat`は候補ごとの値を持たない(Score IRの`snap_candidates`は
    オンセット位置の候補のみでオフセット/長さの候補は保持しない、#25の
    既存スキーマの制約)ため、全候補で同じ`raw_duration_beat`を使う
    (#38の既知の制約としてここに明記)。
    """
    duration_beat = _duration_beats(
        note, note_bar=note_bar, time_signatures=time_signatures, divisions=divisions
    )
    result = []
    for candidate in note.get("snap_candidates", []):
        _, candidate_beat = tick_to_bar_beat(
            candidate["tick"], time_signatures=time_signatures, divisions=divisions
        )
        result.append(
            SnapCandidateInput(
                id=candidate["id"],
                beat=candidate_beat,
                duration_beat=duration_beat,
                grid=candidate["resolution"],
                cost=abs(candidate_beat - note_beat),
            )
        )
    return result


def _should_shrink(
    target_notes: list[dict], *, target_start: int, target_size: int, tempo_map: list[dict]
) -> bool:
    notes_per_bar = len(target_notes) / target_size
    tempo_bpm = _bpm_at_bar(tempo_map, target_start)
    return notes_per_bar > DENSE_NOTES_PER_BAR_THRESHOLD or tempo_bpm > FAST_TEMPO_BPM_THRESHOLD


def build_chunks(score: ScoreIR, part_id: str) -> list[ChunkInput]:
    """1パートを対象に、設計書§7.3のチャンク分割+文脈注入を行う。

    ノートが1件も無いパートは注釈対象が無いため空リストを返す。
    """
    if score.find_part(part_id) is None:
        # `build_song_context_message`(l1_prompt.py)と同じエラー契約に揃える
        # (#38 Gate2レビュー指摘: 以前は`next(...)`が素のStopIterationを
        # 送出しており、呼び出し元にとって原因が分かりにくかった)。
        raise ValueError(f"part {part_id!r} not found in score")

    score_dict: dict[str, Any] = score.model_dump(mode="json")
    divisions = score_dict["divisions"]
    time_signatures = score_dict["time_signatures"]
    tempo_map = score_dict["tempo_map"]
    chords = score_dict["chords"]
    part = next(p for p in score_dict["parts"] if p["id"] == part_id)
    notes = [n for n in part["notes"] if n["status"] != "deleted"]
    if not notes:
        return []

    bar_beat_by_note_id = {
        n["id"]: tick_to_bar_beat(
            n["onset_tick"], time_signatures=time_signatures, divisions=divisions
        )
        for n in notes
    }
    # ノートの「開始」小節の最大値を使う(#38 Gate2レビュー指摘: 終端小節を
    # 使うと、ノートがちょうど小節線上で終わる場合にノートが1つも開始しない
    # 末尾の小節がtargetのチャンクとして生成され、空チャンクがLLM入力に
    # 混入していた)。
    max_bar = max(note_bar for note_bar, _ in bar_beat_by_note_id.values())

    chunks: list[ChunkInput] = []
    bar = 1
    while bar <= max_bar:
        tentative_end = min(bar + DEFAULT_BARS_PER_CHUNK - 1, max_bar)
        tentative_notes = [
            n for n in notes if bar <= bar_beat_by_note_id[n["id"]][0] <= tentative_end
        ]
        size = DEFAULT_BARS_PER_CHUNK
        if _should_shrink(tentative_notes, target_start=bar, target_size=size, tempo_map=tempo_map):
            size = SHRUNK_BARS_PER_CHUNK
        end = min(bar + size - 1, max_bar)

        context_before = [bar - 1] if bar > 1 else []
        context_after = [end + 1] if end + 1 <= max_bar else []
        included_bars = {*context_before, *range(bar, end + 1), *context_after}

        chunk_notes: list[ChunkNote] = []
        for note in notes:
            note_bar, note_beat = bar_beat_by_note_id[note["id"]]
            if note_bar not in included_bars:
                continue
            # §12.4: provenance="user"(手動編集済み)のノートはAIが今後上書き
            # しない設計方針のため、対象小節内であってもeditable=falseとする
            # (#38: コンテキスト小節と同じ扱い)。
            editable = bar <= note_bar <= end and note.get("provenance") != "user"
            chunk_notes.append(
                ChunkNote(
                    id=note["id"],
                    bar=note_bar,
                    raw_beat=note_beat,
                    raw_duration_beat=_duration_beats(
                        note,
                        note_bar=note_bar,
                        time_signatures=time_signatures,
                        divisions=divisions,
                    ),
                    midi=note["midi"],
                    velocity=note["velocity"],
                    confidence=note["confidence"],
                    flags=note["flags"],
                    snap_candidates=_snap_candidates_for_note(
                        note,
                        note_beat=note_beat,
                        note_bar=note_bar,
                        time_signatures=time_signatures,
                        divisions=divisions,
                    ),
                    editable=editable,
                )
            )

        numerator, denominator = time_signature_at_bar(time_signatures, bar)
        target_notes = [n for n in notes if bar <= bar_beat_by_note_id[n["id"]][0] <= end]
        key = estimate_key([n["midi"] % 12 for n in target_notes])
        # #38 Gate2レビュー指摘: chunk_notes同様、context小節(前後1小節)の
        # コード進行もLLMへの参考情報として含める(targetのみに限定すると、
        # 対象小節の判断に必要な直前直後の和声文脈が欠落する)。
        chord_hints = [ChordHint(**c) for c in chords if c["bar"] in included_bars]

        chunks.append(
            ChunkInput(
                context=ChunkContext(
                    key_estimate=key.label,
                    key_confidence=key.confidence,
                    time_signature=f"{numerator}/{denominator}",
                    tempo_bpm=_bpm_at_bar(tempo_map, bar),
                    # `Part`は`instrument`フィールドを持たないため、人間可読な
                    # `name`を使う(#38 Gate2レビュー指摘: 以前は`id`を流用しており、
                    # part.idが"part_1"のような値だとLLMに無意味な楽器名を渡していた)。
                    part=ChunkPart(id=part["id"], instrument=part["name"], staves=part["staves"]),
                    chord_hints=chord_hints,
                    bars=ChunkBars(
                        target=(bar, end),
                        context_before=context_before,
                        context_after=context_after,
                    ),
                ),
                notes=chunk_notes,
            )
        )
        bar = end + 1

    return chunks
