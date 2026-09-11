"""編集オペレーションのUndo/Redo(#32, 設計書§10.4)。

シンボリックな逆操作(例: `note.split`の逆は`note.merge`相当)をop種別ごとに
手書きする代わりに、**ノート単位のスナップショット差分**で実現する:

1. `apply_ops`(`services/score_ops.py`)呼び出し前後で全ノートをスナップショット
2. 値が変わった/新規に現れたnote_idだけを`before`/`after`として差分抽出する
3. Undo = 各`before`へ復元(`before=None`なら新規追加されたノートなのでpartから削除)
4. Redo = 各`after`へ復元(partに無ければ追記、あれば置換)

この方式はop種別が増えても差分ロジック自体は変更不要という利点がある。ノートIDは
`ScoreIR.allocate_note_id()`で単調増加のため、Undo後に別の新規ノートを追加しても
ID衝突しない(削除されたノートのIDが再利用されることはない、§10.1)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.domain.score import Note, ScoreIR
from app.infra import storage


class NoteChange(BaseModel):
    """1ノートぶんの変更前後スナップショット。"""

    part_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class UndoEntry(BaseModel):
    """1回の`/score/ops`呼び出しに対応するUndo/Redo単位。

    `ops`は監査・可読性のために保持する送信されたNoteOpのraw dict(Undo/Redoの
    実行自体はopを再適用せず`changes`のスナップショットのみを使う)。
    """

    ops: list[dict[str, Any]]
    # "user" | "llm" | "agent:run_id"(§10.4)。llm/agentはL1/L2 AI整音・エージェント
    # ランタイム(M4/M5、未実装)向けの将来値のためLiteralではなくstrとする。
    actor: str
    ts: str
    changes: dict[str, NoteChange] = Field(default_factory=dict)


class UndoState(BaseModel):
    """`done`(Undoスタック)と`undone`(Redoスタック)。"""

    done: list[UndoEntry] = Field(default_factory=list)
    undone: list[UndoEntry] = Field(default_factory=list)


def snapshot_notes(score: ScoreIR) -> dict[int, tuple[str, dict[str, Any]]]:
    """全ノートを`{note_id: (part_id, note.model_dump())}`としてスナップショットする。"""
    return {
        note.id: (part.id, note.model_dump(mode="json"))
        for part in score.parts
        for note in part.notes
    }


def diff_snapshots(
    before: dict[int, tuple[str, dict[str, Any]]],
    after: dict[int, tuple[str, dict[str, Any]]],
) -> dict[str, NoteChange]:
    """`before`/`after`のスナップショットを比較し、変化したノートだけを抽出する。"""
    changes: dict[str, NoteChange] = {}
    for note_id in before.keys() | after.keys():
        before_entry = before.get(note_id)
        after_entry = after.get(note_id)
        before_dict = before_entry[1] if before_entry else None
        after_dict = after_entry[1] if after_entry else None
        if before_dict == after_dict:
            continue
        # part_idは変更後(新規追加/更新)を優先し、無ければ変更前(削除相当)から取る。
        # 現状のop集合ではノートがパートを移動することは無いため両者は常に一致する。
        source_entry = after_entry if after_entry is not None else before_entry
        assert source_entry is not None  # note_idはbefore/afterの少なくとも一方に存在する
        part_id = source_entry[0]
        changes[str(note_id)] = NoteChange(part_id=part_id, before=before_dict, after=after_dict)
    return changes


def read_undo_state(path: Path) -> UndoState:
    """`undo_state.json`が無ければ空の`UndoState`を返す。

    #32-M3レビュー指摘: 破損した`undo_state.json`(不正なJSON/スキーマ不一致)に
    対しては`json.JSONDecodeError`/`pydantic.ValidationError`(いずれも
    `ValueError`のサブクラス)が送出されうる。呼び出し元(`api/score.py`の
    `apply_score_ops`)はこれを「undo履歴の永続化に失敗しただけ」として扱いたい
    (スコア本体の書き込み自体は成功しているため500にしたくない)ので、ここで
    握りつぶして空の状態にフォールバックする(壊れたundo履歴を諦めるだけで、
    データ損失はスコア本体には及ばない)。
    """
    if not path.exists():
        return UndoState()
    try:
        return UndoState.model_validate(storage.read_json(path))
    except (OSError, ValueError) as exc:
        print(
            f"[score_undo] warning: undo_state.json is unreadable, resetting: {exc}",
            file=sys.stderr,
        )
        return UndoState()


def write_undo_state(path: Path, state: UndoState) -> None:
    storage.write_json(path, state.model_dump(mode="json"))


def reset_undo_state(path: Path) -> None:
    """#32-M3レビュー指摘: Undoスタックの前提(note_idの集合・意味)を崩す

    パイプラインステージ(transcribe/quantize)の再実行後に呼ぶ。transcribeは
    `provenance="amt"`のノートを丸ごと新しいIDで作り直す(`run_transcribe_stage`
    参照)ため、古いUndoEntryが参照するnote_idは存在しないか無関係になり、
    そのままUndoすると消えたノートを誤って復活させうる。quantizeもuser
    ノートを含む全ノートのonset_tick/duration_tickを再計算するため、古い
    UndoEntryを適用すると再量子化前の位置に巻き戻ってしまう。安全側に倒し、
    ステージ再実行のたびにUndo/Redoスタックをクリアする(監査ログ
    `ops.jsonl`自体は追記専用のまま変更しない)。
    """
    write_undo_state(path, UndoState())


def _upsert_or_remove(
    score: ScoreIR, part_id: str, note_id: int, snapshot: dict[str, Any] | None
) -> None:
    part = score.find_part(part_id)
    if part is None:
        return
    existing = next((n for n in part.notes if n.id == note_id), None)
    if snapshot is None:
        if existing is not None:
            part.notes.remove(existing)
        return
    restored = Note.model_validate(snapshot)
    if existing is not None:
        # 既存のNoteオブジェクトを差し替えず、フィールドをその場で上書きする。
        # 呼び出し元が個々のNoteインスタンスへの参照を保持している場合でも
        # (`score_ops.py`の各`_apply_*`と同じ「既存ノートを直接ミューテートする」
        # 流儀に合わせる)、その参照が最新の状態を反映し続ける。
        for field in Note.model_fields:
            setattr(existing, field, getattr(restored, field))
    else:
        part.notes.append(restored)


def apply_undo(score: ScoreIR, state: UndoState) -> bool:
    """`state.done`の最後のエントリを元に戻す。空なら`False`を返す(no-op)。"""
    if not state.done:
        return False
    entry = state.done.pop()
    for note_id_str, change in entry.changes.items():
        _upsert_or_remove(score, change.part_id, int(note_id_str), change.before)
    state.undone.append(entry)
    return True


def apply_redo(score: ScoreIR, state: UndoState) -> bool:
    """`state.undone`の最後のエントリをやり直す。空なら`False`を返す(no-op)。"""
    if not state.undone:
        return False
    entry = state.undone.pop()
    for note_id_str, change in entry.changes.items():
        _upsert_or_remove(score, change.part_id, int(note_id_str), change.after)
    state.done.append(entry)
    return True


def record_edit(state: UndoState, entry: UndoEntry) -> None:
    """新規編集を`done`へ積み、`undone`(Redoスタック)をクリアする(標準的なUndo/Redo挙動)。"""
    state.done.append(entry)
    state.undone.clear()
