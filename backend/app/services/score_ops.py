"""Score IR編集オペレーションの適用ロジック(#31, FR-09)。

`api/score.py`の`POST /score/ops`から呼ばれる。`apply_ops`は渡された`ScoreIR`を
直接ミューテートする。リクエスト全体のアトミック性(全opが成功するか、
1つでも失敗すれば何も永続化しないか)は呼び出し元が担う責務であり、本モジュール
自身は「渡された対象をその場で変更する」だけである(`services/stage_invalidation.py`
が呼び出し元の書き込み順序に責務を委ねているのと同じ設計思想)。

各opは`Note.provenance = "user"`を付与する: AIは今後このノートを上書きしない
(§10.4)。
"""

from __future__ import annotations

from app.domain.pitch import midi_to_spelling
from app.domain.score import Note, Part, ScoreIR, Spelling
from app.domain.score_ops import (
    NoteAddOp,
    NoteDeleteOp,
    NoteMergeOp,
    NoteOp,
    NoteRestoreOp,
    NoteSplitOp,
    NoteUpdateOp,
    PartTransposeOctaveOp,
)
from app.pipeline.quantize import ticks_to_seconds

# `Note.duration_sec`はPydanticで`gt=0.0`必須(§10.1のモデル制約)。tick→秒変換の
# 丸め等でちょうど0になるのを避けるための下限(#31)。
_MIN_DURATION_SEC = 1e-6

# #31-M3レビュー指摘: midiを新規設定/変更するop(add/update)は`spelling`も
# 必ず設定する(未設定のままだとMusicXMLエクスポートが
# `pipeline/export/score_builder.py`の`_spelling_or_raise`で失敗する)。
# 調号(fifths)は`ScoreIR.key_signatures`だが、このフィールドは現状どの
# パイプラインステージからも設定されない(baseline.py L0のみ`estimate_key_fifths`
# で都度推定し、ScoreIRへは永続化しない)。既存コード全体の前提に合わせ、
# ここでもfifths=0(調号なし相当の異名同音表記)を暫定値として使う。より高度な
# 表記判断はL1 AI整音(M4)に委ねる(`domain/pitch.py`のmidi_to_spellingの
# docstring参照)。


class ScoreOpError(ValueError):
    """opの適用に失敗した(不正なnote_id/範囲外の値等)。API層で422に変換する。"""


def apply_ops(score: ScoreIR, ops: list[NoteOp], beat_anchors: list[tuple[float, float]]) -> None:
    """`ops`を順番に`score`へ適用する。1つでも`ScoreOpError`が出たら即座に中断する

    (以降のopは適用されない)。呼び出し元は`score`のdeep copyに対して呼び、
    成功した場合のみ元のオブジェクトを差し替える/永続化すること。
    """
    for op in ops:
        if isinstance(op, NoteAddOp):
            _apply_add(score, op, beat_anchors)
        elif isinstance(op, NoteUpdateOp):
            _apply_update(score, op, beat_anchors)
        elif isinstance(op, NoteDeleteOp):
            _apply_delete(score, op)
        elif isinstance(op, NoteRestoreOp):
            _apply_restore(score, op)
        elif isinstance(op, NoteSplitOp):
            _apply_split(score, op, beat_anchors)
        elif isinstance(op, NoteMergeOp):
            _apply_merge(score, op, beat_anchors)
        elif isinstance(op, PartTransposeOctaveOp):
            _apply_transpose_octave(score, op)
        else:
            # NoteOpはdiscriminated unionで網羅済みのため実行時には到達しない想定。
            # 将来op種別が追加され本関数の更新を忘れた場合に、無言で無視せず
            # 検出できるようにする(dsp_main.pyのunknown stage処理と同じ考え方)。
            raise ScoreOpError(f"unhandled op type: {op!r}")  # pragma: no cover


def _find_note(score: ScoreIR, note_id: int) -> tuple[Part, Note]:
    for part in score.parts:
        for note in part.notes:
            if note.id == note_id:
                return part, note
    raise ScoreOpError(f"note not found: {note_id}")


def _require_ticks(note: Note) -> tuple[int, int]:
    """編集操作はtick空間で行うため、量子化未実行(tick未設定)のノートは弾く。"""
    if note.onset_tick is None or note.duration_tick is None:
        raise ScoreOpError(
            f"note {note.id} has no onset_tick/duration_tick; run the quantize stage first"
        )
    return note.onset_tick, note.duration_tick


def _resync_timing(
    note: Note, onset_tick: int, end_tick: int, anchors: list[tuple[float, float]]
) -> None:
    """`onset_tick`/`duration_tick`変更後、`onset_sec`/`duration_sec`(生データ)を

    現在のbeatmapとの整合を保つよう再計算する(#31、`ticks_to_seconds`の
    docstring参照: これを怠ると将来のquantize再実行でtick位置が巻き戻る)。
    """
    note.onset_tick = onset_tick
    note.duration_tick = end_tick - onset_tick
    note.onset_sec = ticks_to_seconds(onset_tick, anchors)
    note.duration_sec = max(ticks_to_seconds(end_tick, anchors) - note.onset_sec, _MIN_DURATION_SEC)


def _apply_add(score: ScoreIR, op: NoteAddOp, anchors: list[tuple[float, float]]) -> None:
    part = score.find_part(op.part_id)
    if part is None:
        raise ScoreOpError(f"part not found: {op.part_id!r}")
    if op.staff > part.staves:
        raise ScoreOpError(f"staff {op.staff} exceeds part.staves ({part.staves})")

    note = Note(
        id=score.allocate_note_id(),
        onset_sec=0.0,
        duration_sec=_MIN_DURATION_SEC,
        midi=op.midi,
        velocity=op.velocity,
        voice=op.voice,
        staff=op.staff,
        provenance="user",
        spelling=midi_to_spelling(op.midi),
    )
    _resync_timing(note, op.onset_tick, op.onset_tick + op.duration_tick, anchors)
    part.notes.append(note)


def _spelling_for_new_midi(note: Note, new_midi: int) -> Spelling:
    """`note`のmidiを`new_midi`へ変更する際のspellingを決める。

    #31-M3レビュー指摘: ピッチクラス(`midi % 12`)が変わらないオクターブ
    違いの変更(例: `note.update`でmidiを±12した場合)まで一律fifths=0で
    再計算すると、`_apply_transpose_octave`(既存spellingのoctaveのみ±1する
    方針)と挙動が食い違い、調号由来の表記が失われる。ピッチクラス不変なら
    既存spellingのstep/alterを保ちoctaveだけずらし、ピッチクラス自体が
    変わる場合のみfifths=0で再計算する。
    """
    delta = new_midi - note.midi
    if note.spelling is not None and delta % 12 == 0:
        return note.spelling.model_copy(update={"octave": note.spelling.octave + delta // 12})
    return midi_to_spelling(new_midi)


def _apply_update(score: ScoreIR, op: NoteUpdateOp, anchors: list[tuple[float, float]]) -> None:
    targets = [_find_note(score, note_id) for note_id in op.note_ids]
    for part, note in targets:
        onset_tick, duration_tick = _require_ticks(note)
        new_staff = op.staff if op.staff is not None else note.staff
        if new_staff > part.staves:
            raise ScoreOpError(f"staff {new_staff} exceeds part.staves ({part.staves})")

        if op.onset_tick is not None or op.duration_tick is not None:
            new_onset_tick = op.onset_tick if op.onset_tick is not None else onset_tick
            new_duration_tick = op.duration_tick if op.duration_tick is not None else duration_tick
            _resync_timing(note, new_onset_tick, new_onset_tick + new_duration_tick, anchors)
        if op.midi is not None:
            note.spelling = _spelling_for_new_midi(note, op.midi)
            note.midi = op.midi
        if op.velocity is not None:
            note.velocity = op.velocity
        if op.voice is not None:
            note.voice = op.voice
        if op.staff is not None:
            note.staff = op.staff
        note.provenance = "user"


def _apply_delete(score: ScoreIR, op: NoteDeleteOp) -> None:
    for note_id in op.note_ids:
        _, note = _find_note(score, note_id)
        note.status = "deleted"
        note.provenance = "user"


def _apply_restore(score: ScoreIR, op: NoteRestoreOp) -> None:
    """`_apply_delete`の逆操作(#35)。復元自体もユーザー操作のため

    `provenance="user"`を付与する(削除時と同じ扱い)。

    #35-M3レビュー指摘: 対象が実際に`"deleted"`である場合のみ書き換える。
    ガード無しだと、既に`"active"`なノートへ`note.restore`を送った場合(内容は
    何も変えていない)でも出自が無条件に`"user"`へ上書きされ、AI変更のレビュー
    情報(`provenance_run_id`/`ai_reason`と整合する出自)が失われてしまう。
    """
    for note_id in op.note_ids:
        _, note = _find_note(score, note_id)
        if note.status != "deleted":
            continue
        note.status = "active"
        note.provenance = "user"


def _apply_split(score: ScoreIR, op: NoteSplitOp, anchors: list[tuple[float, float]]) -> None:
    part, note = _find_note(score, op.note_id)
    if note.status != "active":
        raise ScoreOpError(f"note {note.id} is not active; cannot split a deleted/muted note")
    onset_tick, duration_tick = _require_ticks(note)
    end_tick = onset_tick + duration_tick
    if not (onset_tick < op.at_tick < end_tick):
        raise ScoreOpError(
            f"at_tick {op.at_tick} must be strictly within the note's sounding range "
            f"({onset_tick}, {end_tick})"
        )

    new_note = Note(
        id=score.allocate_note_id(),
        onset_sec=0.0,
        duration_sec=_MIN_DURATION_SEC,
        midi=note.midi,
        velocity=note.velocity,
        voice=note.voice,
        staff=note.staff,
        provenance="user",
        spelling=note.spelling if note.spelling is not None else midi_to_spelling(note.midi),
    )
    _resync_timing(new_note, op.at_tick, end_tick, anchors)
    _resync_timing(note, onset_tick, op.at_tick, anchors)
    note.provenance = "user"
    part.notes.append(new_note)


def _apply_merge(score: ScoreIR, op: NoteMergeOp, anchors: list[tuple[float, float]]) -> None:
    found = [_find_note(score, note_id) for note_id in op.note_ids]
    if len({part.id for part, _ in found}) > 1:
        raise ScoreOpError("cannot merge notes from different parts")

    notes = [note for _, note in found]
    if any(n.status != "active" for n in notes):
        # #31-M3レビュー指摘: statusを検証せず結合すると、削除済みノートを
        # 生存ノート(primary)として選んでしまい、結合結果全体が無言で
        # 削除状態のまま残る(§10.1の論理削除方針に反する)。
        raise ScoreOpError("merge requires all target notes to be active")
    if (
        len({n.midi for n in notes}) > 1
        or len({n.voice for n in notes}) > 1
        or len({n.staff for n in notes}) > 1
    ):
        raise ScoreOpError("merge requires notes with the same midi/voice/staff")

    bounds = [_require_ticks(n) for n in notes]
    start_tick = min(onset for onset, _ in bounds)
    end_tick = max(onset + duration for onset, duration in bounds)

    # 最も早く鳴り始めたノートを残す(#31-M3レビュー指摘: id順だと意図が
    # 不明瞭かつUndo/Redo(#32)や差分処理で驚きになりうる)。`bounds`は
    # `notes`と同じ順序で対応するため、Optional型の`onset_tick`を再度
    # 参照せずインデックス経由でmypy安全に選ぶ。
    primary_index = min(range(len(notes)), key=lambda i: (bounds[i][0], notes[i].id))
    primary = notes[primary_index]
    _resync_timing(primary, start_tick, end_tick, anchors)
    primary.provenance = "user"
    for note in notes:
        if note is not primary:
            note.status = "deleted"
            note.provenance = "user"


def _apply_transpose_octave(score: ScoreIR, op: PartTransposeOctaveOp) -> None:
    part = score.find_part(op.part_id)
    if part is None:
        raise ScoreOpError(f"part not found: {op.part_id!r}")

    delta = 12 if op.direction == "up" else -12
    active_notes = [n for n in part.notes if n.status == "active"]
    for note in active_notes:
        if not (0 <= note.midi + delta <= 127):
            raise ScoreOpError(
                f"transposing note {note.id} by {delta} semitones would move midi "
                f"({note.midi}) out of range (0-127)"
            )
    for note in active_notes:
        new_midi = note.midi + delta
        # `_spelling_for_new_midi`はピッチクラス不変(常に12の倍数差である
        # オクターブ移調はこれに該当する)ならstep/alterを保ちoctaveだけ
        # ずらすため、fifths=0前提の再計算による異名同音のズレを避けられる。
        note.spelling = _spelling_for_new_midi(note, new_midi)
        note.midi = new_midi
        note.provenance = "user"
