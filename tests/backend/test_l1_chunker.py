"""#38: `pipeline/refine/l1_chunker.py`(L1チャンク分割+文脈注入)のテスト。

実際のAnthropic API呼び出しは一切発生しない、決定論的なScore IR→チャンクJSON
変換のみを検証する。
"""

from __future__ import annotations

from app.domain.score import (
    ChordEntry,
    Clef,
    Note,
    Part,
    ScoreIR,
    SnapCandidate,
    SourceInfo,
    TempoMapEntry,
    TimeSignatureEntry,
)
from app.pipeline.refine.l1_chunker import (
    DENSE_NOTES_PER_BAR_THRESHOLD,
    build_chunks,
)


def _score(
    *, tempo_bpm: float = 120.0, chords: list[ChordEntry] | None = None
) -> ScoreIR:
    return ScoreIR(
        project_id="p1",
        source=SourceInfo(filename="song.wav", duration_sec=60.0, sample_rate=44100),
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        tempo_map=[TempoMapEntry(bar=1, beat=1.0, bpm=tempo_bpm)],
        chords=chords or [],
    )


def _part(staves: int = 2) -> Part:
    return Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=staves,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )


def _note(
    score: ScoreIR,
    *,
    bar: int,
    midi: int = 60,
    provenance: str = "amt",
    snap_ids: tuple[str, ...] = ("a",),
) -> Note:
    # 4/4, divisions=480固定: 1小節=1920tick。小節頭にノートを1つ置く単純化。
    onset_tick = (bar - 1) * 1920
    return Note(
        id=score.allocate_note_id(),
        onset_sec=onset_tick / 480 * 0.5,
        duration_sec=0.4,
        onset_tick=onset_tick,
        duration_tick=200,
        midi=midi,
        velocity=90,
        provenance=provenance,
        snap_candidates=[
            SnapCandidate(id=sid, resolution="1/8", tick=onset_tick + i * 10, score=1.0)
            for i, sid in enumerate(snap_ids)
        ],
    )


def test_single_bar_part_produces_one_chunk_with_no_context() -> None:
    score = _score()
    part = _part()
    part.notes.append(_note(score, bar=1))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")

    assert len(chunks) == 1
    assert chunks[0].context.bars.target == (1, 1)
    assert chunks[0].context.bars.context_before == []
    assert chunks[0].context.bars.context_after == []


def test_note_with_no_notes_returns_empty_list() -> None:
    score = _score()
    score.parts.append(_part())
    assert build_chunks(score, "piano") == []


def test_ten_bars_splits_into_default_size_chunks_with_shared_context() -> None:
    score = _score()
    part = _part()
    for bar in range(1, 11):
        part.notes.append(_note(score, bar=bar))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")

    targets = [c.context.bars.target for c in chunks]
    assert targets == [(1, 4), (5, 8), (9, 10)]
    # 隣接チャンクはcontext小節を共有する(前チャンクのtarget末尾 == 次チャンクのcontext_before)
    assert chunks[0].context.bars.context_after == [5]
    assert chunks[1].context.bars.context_before == [4]


def test_notes_in_target_bars_are_editable_context_notes_are_not() -> None:
    score = _score()
    part = _part()
    for bar in range(1, 6):
        part.notes.append(_note(score, bar=bar))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    first_chunk = chunks[0]
    editable_by_bar = {n.bar: n.editable for n in first_chunk.notes}
    assert editable_by_bar[1] is True
    assert editable_by_bar[4] is True
    assert editable_by_bar[5] is False  # context_after


def test_user_provenance_note_in_target_range_is_not_editable() -> None:
    """設計書§12.4: provenance=userのノートはAIが今後上書きしない。"""
    score = _score()
    part = _part()
    part.notes.append(_note(score, bar=1, provenance="user"))
    part.notes.append(_note(score, bar=1, provenance="amt"))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    editability = sorted(n.editable for n in chunks[0].notes)
    assert editability == [False, True]


def test_fast_tempo_shrinks_chunk_to_two_bars() -> None:
    score = _score(tempo_bpm=200.0)
    part = _part()
    for bar in range(1, 7):
        part.notes.append(_note(score, bar=bar))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    targets = [c.context.bars.target for c in chunks]
    assert targets == [(1, 2), (3, 4), (5, 6)]


def test_dense_notes_shrinks_chunk_to_two_bars() -> None:
    score = _score()
    part = _part()
    for bar in range(1, 5):
        for _ in range(DENSE_NOTES_PER_BAR_THRESHOLD + 1):
            part.notes.append(_note(score, bar=bar))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    assert chunks[0].context.bars.target == (1, 2)


def test_chord_hints_are_limited_to_target_and_context_bar_range() -> None:
    chords = [
        ChordEntry(bar=1, beat=1.0, symbol="C", confidence=0.9),
        ChordEntry(
            bar=6, beat=1.0, symbol="G", confidence=0.9
        ),  # 遠い小節、含まれない想定
    ]
    score = _score(chords=chords)
    part = _part()
    for bar in range(1, 3):
        part.notes.append(_note(score, bar=bar))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    symbols = [hint.symbol for hint in chunks[0].context.chord_hints]
    assert symbols == ["C"]


def test_chord_hints_in_context_bars_are_included() -> None:
    """回帰(#38 Gate2レビュー指摘): 以前はtarget小節のみに限定しており、

    ノートと同様にcontext小節(前後1小節)の和声文脈も含めるべき。
    """
    chords = [
        ChordEntry(bar=1, beat=1.0, symbol="C", confidence=0.9),
        ChordEntry(bar=5, beat=1.0, symbol="G", confidence=0.9),  # context_after
    ]
    score = _score(chords=chords)
    part = _part()
    for bar in range(1, 6):
        part.notes.append(_note(score, bar=bar))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    assert chunks[0].context.bars.context_after == [5]
    symbols = {hint.symbol for hint in chunks[0].context.chord_hints}
    assert symbols == {"C", "G"}


def test_trailing_bar_with_no_note_starts_does_not_produce_empty_chunk() -> None:
    """回帰(#38 Gate2レビュー指摘): ノートが小節線ちょうどで終わる場合に、

    ノートが1つも開始しない末尾小節がtargetの空チャンクとして生成されないこと。
    """
    score = _score()
    part = _part()
    # 4小節目開始・1920tick(=1小節分)の長さのノート = ちょうど5小節目頭で終わる。
    note = _note(score, bar=4)
    note.duration_tick = 1920
    part.notes.append(note)
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    all_notes = [n for c in chunks for n in c.notes]
    assert all(n.bar <= 4 for n in all_notes if n.editable)
    assert all(len(c.notes) > 0 for c in chunks)


def test_internal_gap_between_sparse_notes_does_not_produce_empty_chunk() -> None:
    """回帰(#38 Gate2レビュー指摘、2巡目): 末尾だけでなく、ノート開始が

    チャンク幅(4小節)以上に空く内部ギャップ(例: 小節1と小節10のみに
    ノートがある場合)でも、対象ノートが1件も無い空チャンクが生成されないこと。
    全チャンクに対して一般化する(単発の`chunks[0]`だけでなく)。
    """
    score = _score()
    part = _part()
    part.notes.append(_note(score, bar=1))
    part.notes.append(_note(score, bar=10))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    assert all(len(c.notes) > 0 for c in chunks)
    target_note_bars = sorted(n.bar for c in chunks for n in c.notes if n.editable)
    assert target_note_bars == [1, 10]


def test_unknown_part_id_raises_value_error() -> None:
    score = _score()
    score.parts.append(_part())
    try:
        build_chunks(score, "nonexistent")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_instrument_uses_part_name_not_id() -> None:
    score = _score()
    part = _part()
    part.id = "part_1"
    part.name = "Piano"
    part.notes.append(_note(score, bar=1))
    score.parts.append(part)

    chunks = build_chunks(score, "part_1")
    assert chunks[0].context.part.instrument == "Piano"


def test_snap_candidate_conversion_preserves_grid_and_computes_cost_from_beat_distance() -> (
    None
):
    score = _score()
    part = _part()
    part.notes.append(_note(score, bar=1, snap_ids=("a", "b")))
    score.parts.append(part)

    chunks = build_chunks(score, "piano")
    note = chunks[0].notes[0]
    assert [c.grid for c in note.snap_candidates] == ["1/8", "1/8"]
    assert all(c.cost >= 0.0 for c in note.snap_candidates)
