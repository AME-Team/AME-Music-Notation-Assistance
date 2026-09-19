"""#41: `pipeline/refine/l1_diff.py`のcurrent vs staged差分計算のテスト。

API層(`api/diff.py`)の配線は`test_diff_api.py`で検証する。ここでは`ScoreIR`を
直接組み立て、`compute_diff`/`apply_change_to_current`/`revert_change_in_staging`の
純粋なロジックのみを検証する。
"""

from __future__ import annotations

from app.domain.score import (
    Clef,
    Note,
    Part,
    ScoreIR,
    SourceInfo,
    Spelling,
    Tie,
    TimeSignatureEntry,
)
from app.pipeline.refine.l1_diff import (
    apply_change_to_current,
    compute_diff,
    filter_by_scope,
    revert_change_in_staging,
)

_RUN_ID = "run_test01"


def _base_score() -> ScoreIR:
    score = ScoreIR(
        project_id="prj_test",
        source=SourceInfo(filename="song.wav", duration_sec=8.0, sample_rate=8000),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )
    score.parts.append(part)
    return score


def _add_note(
    score: ScoreIR, *, onset_tick: int, duration_tick: int = 480, **kwargs
) -> Note:
    part = score.find_part("piano")
    assert part is not None
    note = Note(
        id=score.allocate_note_id(),
        onset_sec=onset_tick / 960.0,
        duration_sec=duration_tick / 960.0,
        onset_tick=onset_tick,
        duration_tick=duration_tick,
        midi=kwargs.pop("midi", 60),
        velocity=90,
        provenance=kwargs.pop("provenance", "baseline"),
        voice=kwargs.pop("voice", 1),
        staff=kwargs.pop("staff", 1),
        **kwargs,
    )
    part.notes.append(note)
    return note


def test_compute_diff_keep_spelling_change() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0)

    staged = current.model_copy(deep=True)
    staged_note = staged.find_part("piano").notes[0]  # type: ignore[union-attr]
    staged_note.spelling = Spelling(step="C", alter=0, octave=4)
    staged_note.provenance = "llm"
    staged_note.provenance_run_id = _RUN_ID
    staged_note.ai_reason = "異名同音を整理"

    changes = compute_diff(current, staged, _RUN_ID)
    assert len(changes) == 1
    change = changes[0]
    assert change.change_type == "keep"
    assert change.note_ids == [1]
    assert change.changed_fields == ["spelling"]
    assert change.bar == 1
    assert change.ai_reason == "異名同音を整理"


def test_compute_diff_delete() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0)

    staged = current.model_copy(deep=True)
    staged_note = staged.find_part("piano").notes[0]  # type: ignore[union-attr]
    staged_note.status = "deleted"
    staged_note.provenance = "llm"
    staged_note.provenance_run_id = _RUN_ID
    staged_note.ai_reason = "ゴースト候補"

    changes = compute_diff(current, staged, _RUN_ID)
    assert len(changes) == 1
    assert changes[0].change_type == "delete"
    assert changes[0].note_ids == [1]


def test_compute_diff_split_tie() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0, duration_tick=960, tie=Tie())

    staged = current.model_copy(deep=True)
    staged_part = staged.find_part("piano")
    assert staged_part is not None
    orig = staged_part.notes[0]
    orig.duration_tick = 480
    orig.duration_sec = 0.5
    orig.tie = Tie(start=False, stop=True)
    orig.provenance = "llm"
    orig.provenance_run_id = _RUN_ID
    orig.ai_reason = "小節境界で分割"

    new_note = Note(
        id=staged.allocate_note_id(),
        onset_sec=0.5,
        duration_sec=0.5,
        onset_tick=480,
        duration_tick=480,
        midi=orig.midi,
        velocity=orig.velocity,
        voice=orig.voice,
        staff=orig.staff,
        tie=Tie(start=True, stop=False),
        provenance="llm",
        provenance_run_id=_RUN_ID,
        ai_reason="小節境界で分割",
    )
    staged_part.notes.append(new_note)

    changes = compute_diff(current, staged, _RUN_ID)
    assert len(changes) == 1
    change = changes[0]
    assert change.change_type == "split_tie"
    assert change.note_ids == [1, 2]
    assert change.after["new_note"]["onset_tick"] == 480


def test_compute_diff_skips_untouched_and_noop_notes() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0)  # id=1: このrunが触れていない
    _add_note(current, onset_tick=480)  # id=2: 別runが触れた

    staged = current.model_copy(deep=True)
    staged_part = staged.find_part("piano")
    assert staged_part is not None
    staged_part.notes[1].provenance = "llm"
    staged_part.notes[1].provenance_run_id = "run_other"

    # このrun_idが触れたノートは無い
    assert compute_diff(current, staged, _RUN_ID) == []

    # 同一run_idでも、値が全く変わっていない(no-op keep)なら差分無し
    staged_part.notes[0].provenance = "llm"
    staged_part.notes[0].provenance_run_id = _RUN_ID
    staged_part.notes[0].ai_reason = "確認済み(変更なし)"
    assert compute_diff(current, staged, _RUN_ID) == []


def test_filter_by_scope() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0)  # bar 1
    _add_note(current, onset_tick=1920)  # bar 2 (4/4, divisions=480 -> 1920 ticks/bar)

    staged = current.model_copy(deep=True)
    for note in staged.find_part("piano").notes:  # type: ignore[union-attr]
        note.spelling = Spelling(step="C", alter=0, octave=4)
        note.provenance = "llm"
        note.provenance_run_id = _RUN_ID

    changes = compute_diff(current, staged, _RUN_ID)
    assert {c.bar for c in changes} == {1, 2}

    only_bar2 = filter_by_scope(changes, part_id=None, bar_range=[2, 2])
    assert [c.bar for c in only_bar2] == [2]

    only_other_part = filter_by_scope(changes, part_id="bass", bar_range=None)
    assert only_other_part == []


def test_apply_change_to_current_keep() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0)

    staged = current.model_copy(deep=True)
    staged_note = staged.find_part("piano").notes[0]  # type: ignore[union-attr]
    staged_note.voice = 2
    staged_note.provenance = "llm"
    staged_note.provenance_run_id = _RUN_ID
    staged_note.ai_reason = "voice分離"

    [change] = compute_diff(current, staged, _RUN_ID)
    apply_change_to_current(change, current=current, staged=staged)

    current_note = current.find_part("piano").notes[0]  # type: ignore[union-attr]
    assert current_note.voice == 2
    assert current_note.provenance == "llm"
    assert current_note.provenance_run_id == _RUN_ID
    assert current_note.ai_reason == "voice分離"

    # 承認後は現在値とstaged値が一致するため、以後の差分取得では再度現れない。
    assert compute_diff(current, staged, _RUN_ID) == []


def test_apply_change_to_current_split_tie_allocates_new_note() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0, duration_tick=960, tie=Tie())
    assert current.next_note_id == 2

    staged = current.model_copy(deep=True)
    staged_part = staged.find_part("piano")
    assert staged_part is not None
    orig = staged_part.notes[0]
    orig.duration_tick = 480
    orig.duration_sec = 0.5
    orig.tie = Tie(start=False, stop=True)
    orig.provenance = "llm"
    orig.provenance_run_id = _RUN_ID
    new_note = Note(
        id=staged.allocate_note_id(),
        onset_sec=0.5,
        duration_sec=0.5,
        onset_tick=480,
        duration_tick=480,
        midi=orig.midi,
        velocity=orig.velocity,
        voice=orig.voice,
        staff=orig.staff,
        tie=Tie(start=True, stop=False),
        provenance="llm",
        provenance_run_id=_RUN_ID,
    )
    staged_part.notes.append(new_note)

    [change] = compute_diff(current, staged, _RUN_ID)
    apply_change_to_current(change, current=current, staged=staged)

    current_part = current.find_part("piano")
    assert current_part is not None
    assert [n.id for n in current_part.notes] == [1, 2]
    assert current_part.notes[0].duration_tick == 480
    assert current_part.notes[1].onset_tick == 480
    # 次回`allocate_note_id`がid=2と衝突しないよう更新されていること。
    assert current.next_note_id == 3


def test_revert_change_in_staging_clears_diff() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0)

    staged = current.model_copy(deep=True)
    staged_note = staged.find_part("piano").notes[0]  # type: ignore[union-attr]
    staged_note.staff = 2
    staged_note.provenance = "llm"
    staged_note.provenance_run_id = _RUN_ID

    [change] = compute_diff(current, staged, _RUN_ID)
    revert_change_in_staging(change, current=current, staged=staged)

    assert compute_diff(current, staged, _RUN_ID) == []
    reverted_note = staged.find_part("piano").notes[0]  # type: ignore[union-attr]
    assert reverted_note.staff == 1
    assert reverted_note.provenance_run_id is None


def test_revert_change_in_staging_split_tie_removes_new_note() -> None:
    current = _base_score()
    _add_note(current, onset_tick=0, duration_tick=960, tie=Tie())

    staged = current.model_copy(deep=True)
    staged_part = staged.find_part("piano")
    assert staged_part is not None
    orig = staged_part.notes[0]
    orig.duration_tick = 480
    orig.duration_sec = 0.5
    orig.tie = Tie(start=False, stop=True)
    orig.provenance = "llm"
    orig.provenance_run_id = _RUN_ID
    new_note = Note(
        id=staged.allocate_note_id(),
        onset_sec=0.5,
        duration_sec=0.5,
        onset_tick=480,
        duration_tick=480,
        midi=orig.midi,
        velocity=orig.velocity,
        voice=orig.voice,
        staff=orig.staff,
        tie=Tie(start=True, stop=False),
        provenance="llm",
        provenance_run_id=_RUN_ID,
    )
    staged_part.notes.append(new_note)

    [change] = compute_diff(current, staged, _RUN_ID)
    revert_change_in_staging(change, current=current, staged=staged)

    assert compute_diff(current, staged, _RUN_ID) == []
    assert [n.id for n in staged_part.notes] == [1]
    assert staged_part.notes[0].duration_tick == 960
