"""#39: `pipeline/refine/l1_runner.py`(L1逐次モードのオーケストレーション)のテスト。

`call_l1_chunk`(実際のAnthropic API呼び出しを行う関数)は常にモックし、
実ネットワーク呼び出しは一切発生させない。検証層(V-1〜V-9)との連携・
decision種別ごとの適用ロジック・元の`score`が変更されないことを検証する。
"""

from __future__ import annotations

from unittest.mock import patch

from app.domain.invariants import Decision
from app.domain.score import (
    Clef,
    Note,
    Part,
    ScoreIR,
    SnapCandidate,
    SourceInfo,
    Spelling,
    TempoMapEntry,
    TimeSignatureEntry,
)
from app.pipeline.refine.l1_client import L1ChunkCallResult, L1ChunkResponse
from app.pipeline.refine.l1_runner import run_l1_sequential

# divisions=480, 4/4: 1拍=480tick、1小節=1920tick。tick 480/sec(簡略化した一定レート)。
_BEAT_ANCHORS = [(0.0, 0.0), (100.0, 48000.0)]


def _score() -> ScoreIR:
    return ScoreIR(
        project_id="p1",
        source=SourceInfo(filename="song.wav", duration_sec=60.0, sample_rate=44100),
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        tempo_map=[TempoMapEntry(bar=1, beat=1.0, bpm=120.0)],
    )


def _part() -> Part:
    return Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )


def _note(
    score: ScoreIR,
    *,
    onset_tick: int = 0,
    duration_tick: int = 480,
    midi: int = 60,
    voice: int = 1,
) -> Note:
    return Note(
        id=score.allocate_note_id(),
        onset_sec=onset_tick / 480.0,
        duration_sec=duration_tick / 480.0,
        onset_tick=onset_tick,
        duration_tick=duration_tick,
        midi=midi,
        velocity=90,
        voice=voice,
        provenance="amt",
        spelling=Spelling(step="C", alter=0, octave=4),
        snap_candidates=[
            SnapCandidate(id="a", resolution="1/8", tick=onset_tick, score=1.0),
            SnapCandidate(id="b", resolution="1/16", tick=onset_tick + 120, score=0.5),
        ],
    )


def _staged_notes(result, part_id: str = "piano") -> list[Note]:
    part = result.staged_score.find_part(part_id)
    assert part is not None
    return part.notes


def _mock_call(decisions: list[Decision], usage: dict | None = None):
    result = L1ChunkCallResult(
        output=L1ChunkResponse(bar_range=(1, 1), decisions=decisions),
        usage=usage
        or {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
    )
    return patch("app.pipeline.refine.l1_runner.call_l1_chunk", return_value=result)


def test_chunk_rejected_on_validation_violation_leaves_note_unchanged() -> None:
    score = _score()
    part = _part()
    note = _note(score)
    part.notes.append(note)
    score.parts.append(part)

    # snap候補に存在しないIDを指定 → V-3違反 → チャンク全体棄却。
    bad_decision = Decision(
        note_id=note.id,
        action="keep",
        snap="nonexistent",
        spelling=note.spelling,
        reason="bad",
    )

    with _mock_call([bad_decision]):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_test",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    assert result.chunks_ok == 0
    assert result.chunks_rejected == 1
    assert len(result.rejected_reasons) == 1
    staged_note = _staged_notes(result)[0]
    assert staged_note.provenance == "amt"  # 変更されていない


def test_original_score_is_not_mutated() -> None:
    score = _score()
    part = _part()
    note = _note(score)
    part.notes.append(note)
    score.parts.append(part)

    decision = Decision(
        note_id=note.id,
        action="keep",
        snap="a",
        spelling=Spelling(step="D", alter=0, octave=4),
        voice=1,
        staff=1,
        reason="ok",
    )

    with _mock_call([decision]):
        run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_test",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    assert part.notes[0].provenance == "amt"  # 元のscoreは無傷


def test_keep_decision_updates_spelling_voice_staff_and_provenance() -> None:
    score = _score()
    part = _part()
    note = _note(score)
    part.notes.append(note)
    score.parts.append(part)

    # B#3はmidi 60(C4)とピッチクラス・絶対音高ともに一致する有効な異名同音
    # (spelling_to_midi((3+1)*12+11+1)=60)。単なるletter違いではなく、実際に
    # 検証を通る現実的な「AIが異なる表記を選ぶ」ケースとして使う。
    b_sharp = Spelling(step="B", alter=1, octave=3)
    decision = Decision(
        note_id=note.id,
        action="keep",
        snap="b",
        spelling=b_sharp,
        voice=2,
        staff=2,
        reason="voice separation",
    )

    with _mock_call([decision]):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    assert result.chunks_ok == 1
    staged_note = _staged_notes(result)[0]
    assert staged_note.spelling == b_sharp
    assert staged_note.voice == 2
    assert staged_note.staff == 2
    assert staged_note.provenance == "llm"
    assert staged_note.provenance_run_id == "run_abc"
    assert staged_note.ai_reason == "voice separation"
    assert staged_note.onset_tick == 120  # snap "b" のtick


def test_delete_decision_sets_status_and_provenance() -> None:
    score = _score()
    part = _part()
    note = _note(score)
    part.notes.append(note)
    # V-6(delete率、既定15%)に抵触しないよう、削除しないノートを追加して
    # 母数を増やす(1件のみだと100%delete率になりチャンクごと棄却されるため)。
    # 小節1(tick 0〜1919)に収まり、かつ`note`(tick 0〜480)や互いと重ならない
    # よう間隔・長さを選ぶ(#37 Gate2の暗黙keep対応により、decision無しの
    # ノート同士もV-8の重複チェック対象になっているため)。
    for i in range(9):
        part.notes.append(_note(score, onset_tick=500 + i * 100, duration_tick=90))
    score.parts.append(part)

    decision = Decision(note_id=note.id, action="delete", reason="ghost note")

    with _mock_call([decision]):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    staged_note = _staged_notes(result)[0]
    assert staged_note.status == "deleted"
    assert staged_note.provenance == "llm"
    assert staged_note.ai_reason == "ghost note"


def test_split_tie_creates_new_note_with_correct_tie_flags() -> None:
    score = _score()
    part = _part()
    # 小節1のtick 0〜960(2拍分)のノート。beat 2.0(tick 480)で分割する。
    note = _note(score, onset_tick=0, duration_tick=960)
    part.notes.append(note)
    score.parts.append(part)

    decision = Decision(
        note_id=note.id,
        action="split_tie",
        snap="a",
        split_at_beat=2.0,
        spelling=note.spelling,
        voice=1,
        staff=1,
        reason="小節またぎ",
    )

    with _mock_call([decision]):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    staged_notes = _staged_notes(result)
    assert len(staged_notes) == 2
    first, second = sorted(staged_notes, key=lambda n: n.onset_tick or 0)
    assert first.onset_tick == 0
    assert first.duration_tick == 480
    assert first.tie.stop is True
    assert second.onset_tick == 480
    assert second.duration_tick == 480
    assert second.tie.start is True
    assert second.provenance == "llm"


def test_voice_reassignment_causing_post_apply_overlap_is_rejected() -> None:
    """回帰(#39 Gate2レビュー指摘、2巡目): V-1〜V-9は適用前の(raw_beat・元の

    voiceの)状態に対して検証していたため、`decision.voice`でvoiceを変更した
    結果、適用後に別ノートと同一voice内で時間重複が生じるケースを見逃して
    いた。2つの重ならないノート(voice 1とvoice 2)のうちvoice 2の方をvoice 1
    へ再割り当てするdecisionは、適用後に重複するためV-8違反としてチャンク
    全体が棄却されるべき。
    """
    score = _score()
    part = _part()
    note_a = _note(score, onset_tick=0, duration_tick=480, voice=1)
    note_b = _note(score, onset_tick=0, duration_tick=480, voice=2)
    part.notes.append(note_a)
    part.notes.append(note_b)
    score.parts.append(part)

    # note_bをvoice 1へ再割り当て → note_aと同一voice・同一時間区間で重複する。
    decision = Decision(
        note_id=note_b.id,
        action="keep",
        snap="a",
        spelling=note_b.spelling,
        voice=1,
        staff=1,
        reason="voiceの統一",
    )

    with _mock_call([decision]):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    assert result.chunks_ok == 0
    assert result.chunks_rejected == 1
    assert any("V-8" in reason for reason in result.rejected_reasons)
    staged_note_b = next(n for n in _staged_notes(result) if n.id == note_b.id)
    assert staged_note_b.voice == 2  # 棄却されたため変更されていない


def test_merge_with_previous_is_skipped_and_logged() -> None:
    score = _score()
    part = _part()
    note = _note(score)
    part.notes.append(note)
    score.parts.append(part)

    decision = Decision(
        note_id=note.id, action="merge_with_previous", reason="repeated note"
    )

    with _mock_call([decision]):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    assert result.chunks_ok == 1  # チャンク自体は棄却されない
    assert len(result.skipped_decisions) == 1
    staged_note = _staged_notes(result)[0]
    assert staged_note.provenance == "amt"  # 何も適用されていない


def test_usage_accumulates_and_is_recorded_in_meta() -> None:
    score = _score()
    part = _part()
    note = _note(score)
    part.notes.append(note)
    score.parts.append(part)

    decision = Decision(note_id=note.id, action="delete", reason="x")

    with _mock_call(
        [decision],
        usage={"input_tokens": 111, "output_tokens": 22, "cache_read_input_tokens": 3},
    ):
        result = run_l1_sequential(
            score,
            "piano",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )

    assert result.usage == {
        "input_tokens": 111,
        "output_tokens": 22,
        "cache_read_input_tokens": 3,
    }
    assert result.staged_score.meta.stages["refine"]["l1"]["usage"] == result.usage
    assert result.staged_score.meta.stages["refine"]["lanes_applied"] == ["L0", "L1"]


def test_unknown_part_id_raises_value_error() -> None:
    score = _score()
    score.parts.append(_part())
    import pytest

    with pytest.raises(ValueError):
        run_l1_sequential(
            score,
            "nonexistent",
            client=object(),
            run_id="run_abc",
            beat_anchors=_BEAT_ANCHORS,
            model="claude-opus-5",
            effort="high",
        )
