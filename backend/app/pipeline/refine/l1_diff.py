"""L1ステージング vs current.jsonの差分計算(#41, 設計書§4.1 FR-10/§10.4/§11.3)。

L1(#39/#40)は`current.json`を変更せず`score/staging/{run_id}.json`へ提案を保存する
(`api/refine.py`のdocstring参照)。本モジュールは両者を突き合わせ、その`run_id`が
実際に変更したノート(`Note.provenance_run_id == run_id`)だけを対象に、DiffPanel
向けの変更一覧を組み立てる。

`Decision`(L1呼び出し時の生の判断)はここでは参照しない: `current`/`staged`の
ノート値そのものを比較する方式にすることで、L1側にDecisionを永続化させる必要が
なくなる(#39/#40時点でDecisionはどこにも保存されていない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.domain.score import Note, ScoreIR
from app.pipeline.time_signature import tick_to_bar_beat

NoteChangeType = Literal["keep", "delete", "split_tie"]

# `Note`のうち、L1の`keep`決定(snap移動/spelling/voice/staff/tie変更)が触りうる
# フィールド。durationは`keep`では変化しない(`l1_runner._apply_keep`のdocstring
# 参照: snap候補はduration別の値を持たないため)。
_COMPARABLE_FIELDS: tuple[str, ...] = ("onset_tick", "spelling", "voice", "staff", "tie")


@dataclass(frozen=True)
class NoteDiffChange:
    """DiffPanel向けの変更1件(小節・ノート単位)。"""

    change_type: NoteChangeType
    part_id: str
    bar: int
    note_ids: list[int]
    changed_fields: list[str]
    midi: int
    before: dict[str, Any]
    after: dict[str, Any]
    ai_reason: str | None


def _note_bar(note: Note, *, score: ScoreIR) -> int:
    bar, _ = tick_to_bar_beat(
        note.onset_tick or 0,
        time_signatures=[ts.model_dump(mode="json") for ts in score.time_signatures],
        divisions=score.divisions,
    )
    return bar


def _field_snapshot(note: Note) -> dict[str, Any]:
    return {
        "onset_tick": note.onset_tick,
        "spelling": note.spelling.model_dump(mode="json") if note.spelling is not None else None,
        "voice": note.voice,
        "staff": note.staff,
        "tie": note.tie.model_dump(mode="json"),
        "status": note.status,
    }


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [f for f in _COMPARABLE_FIELDS if before[f] != after[f]]


def _copy_fields(dest: Note, src: Note) -> None:
    """`src`の編集可能フィールドを`dest`へコピーする(承認/却下双方向で使う)。

    `duration_tick`/`duration_sec`はここに含めない: `keep`では変化せず、
    `split_tie`は呼び出し元が別途明示的にコピーする(orig側のみ短縮される、
    newノートは丸ごと複製するため)。
    """
    dest.onset_tick = src.onset_tick
    dest.onset_sec = src.onset_sec
    dest.spelling = src.spelling
    dest.voice = src.voice
    dest.staff = src.staff
    dest.tie = src.tie
    dest.provenance = src.provenance
    dest.provenance_run_id = src.provenance_run_id
    dest.ai_reason = src.ai_reason


def _find_split_new_note(
    orig: Note, staged_notes: list[Note], *, run_id: str, current_ids: set[int]
) -> Note | None:
    """`orig`を`split_tie`した結果生まれた新規ノートを探す(`l1_runner._apply_split_tie`参照)。

    新規ノートは`current`にまだ存在せず(#41時点でid未割当)、`orig`の終了tickの
    位置から始まり、同じmidiを持つ(分割は音高を変えない)。
    """
    if orig.onset_tick is None or orig.duration_tick is None:
        return None
    end_tick = orig.onset_tick + orig.duration_tick
    for candidate in staged_notes:
        if (
            candidate.id not in current_ids
            and candidate.provenance_run_id == run_id
            and candidate.onset_tick == end_tick
            and candidate.midi == orig.midi
        ):
            return candidate
    return None


def compute_diff(current: ScoreIR, staged: ScoreIR, run_id: str) -> list[NoteDiffChange]:
    """`current`と`staged`(`score/staging/{run_id}.json`)を比較する。

    `staged_note.provenance_run_id != run_id`のノート(このrunが触れていない、
    またはL0のまま)は対象外。実質的な差分が無い`keep`決定(全フィールド一致)も
    除外する(#41着手前にユーザーと合意: 承認/却下後にstaged側へ書き戻す設計と
    組み合わせ、一度処理した変更が再度「未処理」として現れないようにするため)。
    """
    changes: list[NoteDiffChange] = []
    for staged_part in staged.parts:
        current_part = current.find_part(staged_part.id)
        if current_part is None:
            continue
        current_by_id = {n.id: n for n in current_part.notes}
        current_ids = set(current_by_id)
        consumed: set[int] = set()

        for staged_note in staged_part.notes:
            if staged_note.id not in current_ids or staged_note.id in consumed:
                continue
            if staged_note.provenance_run_id != run_id:
                continue
            current_note = current_by_id[staged_note.id]

            new_note = _find_split_new_note(
                staged_note, staged_part.notes, run_id=run_id, current_ids=current_ids
            )
            if new_note is not None:
                consumed.add(staged_note.id)
                consumed.add(new_note.id)
                changes.append(
                    NoteDiffChange(
                        change_type="split_tie",
                        part_id=staged_part.id,
                        bar=_note_bar(current_note, score=current),
                        note_ids=[staged_note.id, new_note.id],
                        changed_fields=["duration_tick", "tie"],
                        midi=staged_note.midi,
                        before={
                            "duration_tick": current_note.duration_tick,
                            "tie": current_note.tie.model_dump(mode="json"),
                        },
                        after={
                            "duration_tick": staged_note.duration_tick,
                            "tie": staged_note.tie.model_dump(mode="json"),
                            "new_note": {
                                "onset_tick": new_note.onset_tick,
                                "duration_tick": new_note.duration_tick,
                                "midi": new_note.midi,
                                "voice": new_note.voice,
                                "staff": new_note.staff,
                                "spelling": (
                                    new_note.spelling.model_dump(mode="json")
                                    if new_note.spelling is not None
                                    else None
                                ),
                                "tie": new_note.tie.model_dump(mode="json"),
                            },
                        },
                        ai_reason=staged_note.ai_reason,
                    )
                )
                continue

            if staged_note.status == "deleted" and current_note.status != "deleted":
                changes.append(
                    NoteDiffChange(
                        change_type="delete",
                        part_id=staged_part.id,
                        bar=_note_bar(current_note, score=current),
                        note_ids=[staged_note.id],
                        changed_fields=["status"],
                        midi=staged_note.midi,
                        before={"status": current_note.status},
                        after={"status": staged_note.status},
                        ai_reason=staged_note.ai_reason,
                    )
                )
                continue

            before = _field_snapshot(current_note)
            after = _field_snapshot(staged_note)
            changed = _changed_fields(before, after)
            if not changed:
                continue
            changes.append(
                NoteDiffChange(
                    change_type="keep",
                    part_id=staged_part.id,
                    bar=_note_bar(current_note, score=current),
                    note_ids=[staged_note.id],
                    changed_fields=changed,
                    midi=staged_note.midi,
                    before=before,
                    after=after,
                    ai_reason=staged_note.ai_reason,
                )
            )
    return changes


def filter_by_scope(
    changes: list[NoteDiffChange], *, part_id: str | None, bar_range: list[int] | None
) -> list[NoteDiffChange]:
    """`part_id`/`bar_range`(両端含む)でスコープを絞り込む。両方`None`なら全件。"""
    result = changes
    if part_id is not None:
        result = [c for c in result if c.part_id == part_id]
    if bar_range is not None:
        lo, hi = bar_range
        result = [c for c in result if lo <= c.bar <= hi]
    return result


def apply_change_to_current(change: NoteDiffChange, *, current: ScoreIR, staged: ScoreIR) -> None:
    """承認: `change`が示す`staged`側の値を`current`へ適用する。"""
    part = current.find_part(change.part_id)
    staged_part = staged.find_part(change.part_id)
    assert part is not None
    assert staged_part is not None
    notes_by_id = {n.id: n for n in part.notes}
    staged_by_id = {n.id: n for n in staged_part.notes}

    if change.change_type == "split_tie":
        orig_id, new_id = change.note_ids
        orig = notes_by_id[orig_id]
        staged_orig = staged_by_id[orig_id]
        staged_new = staged_by_id[new_id]
        _copy_fields(orig, staged_orig)
        orig.duration_tick = staged_orig.duration_tick
        orig.duration_sec = staged_orig.duration_sec
        part.notes.append(staged_new.model_copy(deep=True))
        current.next_note_id = max(current.next_note_id, new_id + 1)
        return

    note = notes_by_id[change.note_ids[0]]
    staged_note = staged_by_id[note.id]
    if change.change_type == "delete":
        note.status = staged_note.status
        note.provenance = staged_note.provenance
        note.provenance_run_id = staged_note.provenance_run_id
        note.ai_reason = staged_note.ai_reason
        return

    _copy_fields(note, staged_note)


def revert_change_in_staging(change: NoteDiffChange, *, current: ScoreIR, staged: ScoreIR) -> None:
    """却下: `staged`側の該当ノートを`current`相当の値に書き戻し、差分を消す。

    (#41着手前にユーザーと合意: 却下はcurrentを変更しないため、staged側を
    「このrunが触れる前の状態」に戻すことで、以後の差分取得で再度「未処理」
    として現れないようにする。)
    """
    staged_part = staged.find_part(change.part_id)
    current_part = current.find_part(change.part_id)
    assert staged_part is not None
    assert current_part is not None
    staged_by_id = {n.id: n for n in staged_part.notes}
    current_by_id = {n.id: n for n in current_part.notes}

    if change.change_type == "split_tie":
        orig_id, new_id = change.note_ids
        staged_orig = staged_by_id[orig_id]
        current_orig = current_by_id[orig_id]
        _copy_fields(staged_orig, current_orig)
        staged_orig.duration_tick = current_orig.duration_tick
        staged_orig.duration_sec = current_orig.duration_sec
        staged_part.notes[:] = [n for n in staged_part.notes if n.id != new_id]
        return

    staged_note = staged_by_id[change.note_ids[0]]
    current_note = current_by_id[staged_note.id]
    if change.change_type == "delete":
        staged_note.status = current_note.status
        staged_note.provenance = current_note.provenance
        staged_note.provenance_run_id = current_note.provenance_run_id
        staged_note.ai_reason = current_note.ai_reason
        return

    _copy_fields(staged_note, current_note)
