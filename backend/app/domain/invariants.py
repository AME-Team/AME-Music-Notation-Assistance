"""検証層 V-1〜V-10(#37, 設計書§9)。

L1では門番(違反したチャンクを棄却)として、L2ではMCPツール(構造化した違反内容を
返してエージェントに自己修正させる)として使われる。**実装はここ1つに集約し、両者で
共有する**(§9)。呼び出し側の挙動分岐(チャンク棄却 vs エラー応答)はこのモジュールの
責務ではなく、`validate_decisions`は常に構造化された違反リストを返すだけに留める。

`domain/`は外部ライブラリに依存しない方針(`domain/score.py`と同様、スキーマ定義の
ためのPydanticは意図的な例外として許容する、#23)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.pitch import is_spelling_consistent_with_midi, spelling_pitch_class
from app.domain.score import Spelling, Tie

DecisionAction = Literal["keep", "delete", "split_tie", "merge_with_previous"]

# V-8での時間占有チェック対象(deleteは消滅、merge_with_previousは直前ノートに
# 吸収されるため、いずれもそれ自体としては時間を占有しない、#37設計判断)。
_TIME_OCCUPYING_ACTIONS: frozenset[DecisionAction] = frozenset({"keep", "split_tie"})
# V-3(snap候補チェック)の対象(deleteはsnapを持たない。merge_with_previousも
# 対象ノート自体の位置は消えるため対象外)。
_SNAP_REQUIRING_ACTIONS: frozenset[DecisionAction] = frozenset({"keep", "split_tie"})

DEFAULT_MAX_DELETE_RATE = 0.15
DEFAULT_MAX_GHOST_DELETE_RATE = 0.6


class Decision(BaseModel):
    """L1/L2がノート1件に対して下す判断(設計書§7.3の出力スキーマ`decisions[]`)。"""

    model_config = ConfigDict(extra="forbid")

    note_id: int
    action: DecisionAction
    snap: str | None = None
    spelling: Spelling | None = None
    voice: int | None = None
    staff: int | None = None
    tie: Tie | None = None
    split_at_beat: float | None = None
    reason: str


class ValidationNote(BaseModel):
    """検証に必要な最小限のノート情報。L1チャンク入力の`notes[]`に対応する(§7.3)。

    `onset_beat`/`duration_beat`は`raw_beat`/`raw_duration_beat`と同じ単位
    (1拍=四分音符)。ticksではなくbeatを使うのは、`split_at_beat`との比較に
    そのまま使え、`divisions`(ticks/beat)をdomain層に持ち込まずに済むため。
    """

    id: int
    editable: bool
    midi: int
    onset_beat: float
    duration_beat: float
    snap_candidate_ids: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Violation:
    """検証違反1件(§8.4の`{rule, note_id, message}`形式)。"""

    rule: str  # "V-1".."V-10"
    note_id: int
    message: str


def _delete_rate_violations(
    decisions: list[Decision],
    notes_by_id: dict[int, ValidationNote],
    *,
    max_delete_rate: float,
    max_ghost_delete_rate: float,
) -> list[Violation]:
    """V-6: delete率が閾値以下(既定15%、ghost_candidate付きは60%)。

    ghost_candidateフラグの有無で対象ノートを2群に分け、各群で独立にdelete率を
    判定する(ghost候補は元々削除されやすい性質のため、より高い閾値を許容する
    という設計意図、§9)。
    """
    decision_by_id = {d.note_id: d for d in decisions}
    editable_notes = [n for n in notes_by_id.values() if n.editable]
    ghost_ids = [n.id for n in editable_notes if "ghost_candidate" in n.flags]
    plain_ids = [n.id for n in editable_notes if "ghost_candidate" not in n.flags]

    violations: list[Violation] = []
    for ids, threshold, label in (
        (plain_ids, max_delete_rate, "非ghost"),
        (ghost_ids, max_ghost_delete_rate, "ghost_candidate付き"),
    ):
        if not ids:
            continue
        deleted = [
            i for i in ids if (d := decision_by_id.get(i)) is not None and d.action == "delete"
        ]
        rate = len(deleted) / len(ids)
        if rate > threshold:
            violations.append(
                Violation(
                    rule="V-6",
                    note_id=ids[0],
                    message=(
                        f"{label}ノートのdelete率{rate:.0%}が閾値{threshold:.0%}を超過"
                        f"({len(deleted)}/{len(ids)}件)"
                    ),
                )
            )
    return violations


def _overlap_violations(
    decisions: list[Decision], notes_by_id: dict[int, ValidationNote]
) -> list[Violation]:
    """V-8: 同一voice内でノートが時間的に重複しない。

    delete/merge_with_previousは占有区間から除外する(それ自体は時間を占有
    しなくなるため、#37設計判断)。同一voice内で区間が重なるペアを検出する。
    """
    by_voice: dict[int, list[tuple[float, float, int]]] = {}
    for decision in decisions:
        if decision.action not in _TIME_OCCUPYING_ACTIONS or decision.voice is None:
            continue
        note = notes_by_id.get(decision.note_id)
        if note is None:
            continue
        by_voice.setdefault(decision.voice, []).append(
            (note.onset_beat, note.onset_beat + note.duration_beat, note.id)
        )

    violations: list[Violation] = []
    for intervals in by_voice.values():
        ordered = sorted(intervals, key=lambda item: item[0])
        for (_, end, _), (next_start, _, next_id) in zip(ordered, ordered[1:], strict=False):
            if next_start < end:
                violations.append(
                    Violation(
                        rule="V-8",
                        note_id=next_id,
                        message=(
                            f"同一voice内でノート{next_id}(開始{next_start})が"
                            f"直前ノートの発音区間(終了{end})と重複"
                        ),
                    )
                )
    return violations


def validate_decisions(
    decisions: list[Decision],
    *,
    notes: list[ValidationNote],
    part_staves: int,
    max_delete_rate: float = DEFAULT_MAX_DELETE_RATE,
    max_ghost_delete_rate: float = DEFAULT_MAX_GHOST_DELETE_RATE,
) -> list[Violation]:
    """V-1〜V-9を検証し、違反を構造化して返す(§9)。

    `notes`はチャンク入力の全ノート(対象小節+コンテキスト小節)。**V-10(decisionが
    無い入力ノートは暗黙keep)はここでは検証しない** — 「違反」ではなく「decisionで
    網羅されていないノートはL0の結果をそのまま採用する」という呼び出し側の既定動作
    そのものであり、`decisions`が`notes`の全editableノートを網羅している必要はない
    (これ自体を保証するテストを別途用意する)。

    戻り値は常にリスト(空リストなら違反なし)であり、例外は投げない。検証失敗を
    握り潰さずログ/UIに表示する経路(NFR-06)は、この戻り値を握り潰さず伝播させる
    呼び出し側(#39のL1呼び出し等)の責務。
    """
    notes_by_id = {n.id: n for n in notes}
    violations: list[Violation] = []

    for decision in decisions:
        note = notes_by_id.get(decision.note_id)
        if note is None:
            # V-1: 対象スコープ(チャンク入力)に存在しないnote_idへの言及。
            # 実在しないID=ハルシネーションであり、V-2(存在するがcontext)とは
            # 区別する(Issue #37本文はV-1に「存在しeditable:true」を一括りに
            # 書いているが、V-2が独立ルールとして存在する以上、「不在」と
            # 「存在するがcontext」を分けて判定するのが整合的な解釈と判断)。
            violations.append(
                Violation(
                    rule="V-1",
                    note_id=decision.note_id,
                    message="対象スコープに存在しないnote_id",
                )
            )
            continue

        if not note.editable:
            # V-2: 実在するがeditable=false(コンテキスト小節)のノートへの言及。
            violations.append(
                Violation(
                    rule="V-2",
                    note_id=decision.note_id,
                    message="editable=falseのコンテキストノートへの言及",
                )
            )
            continue

        if decision.action in _SNAP_REQUIRING_ACTIONS and (
            decision.snap is None or decision.snap not in note.snap_candidate_ids
        ):
            violations.append(
                Violation(
                    rule="V-3",
                    note_id=decision.note_id,
                    message=(f"snap '{decision.snap}' が候補{note.snap_candidate_ids}に含まれない"),
                )
            )

        if decision.spelling is not None:
            if spelling_pitch_class(decision.spelling) != note.midi % 12:
                violations.append(
                    Violation(
                        rule="V-4",
                        note_id=decision.note_id,
                        message=(
                            f"spellingのピッチクラスがmidi {note.midi}"
                            f"(%12={note.midi % 12})と不一致"
                        ),
                    )
                )
            elif not is_spelling_consistent_with_midi(decision.spelling, note.midi):
                violations.append(
                    Violation(
                        rule="V-5",
                        note_id=decision.note_id,
                        message=f"spellingのoctaveがmidi {note.midi}と不整合",
                    )
                )

        if decision.voice is not None and not (1 <= decision.voice <= 4):
            violations.append(
                Violation(
                    rule="V-7",
                    note_id=decision.note_id,
                    message=f"voice {decision.voice} は1〜4の範囲外",
                )
            )
        if decision.staff is not None and not (1 <= decision.staff <= part_staves):
            violations.append(
                Violation(
                    rule="V-7",
                    note_id=decision.note_id,
                    message=f"staff {decision.staff} は1〜{part_staves}の範囲外",
                )
            )

        note_end = note.onset_beat + note.duration_beat
        if (
            decision.action == "split_tie"
            and decision.split_at_beat is not None
            and not (note.onset_beat <= decision.split_at_beat <= note_end)
        ):
            violations.append(
                Violation(
                    rule="V-9",
                    note_id=decision.note_id,
                    message=(
                        f"split_at_beat {decision.split_at_beat} が発音区間"
                        f"[{note.onset_beat}, {note_end}]の範囲外"
                    ),
                )
            )

    violations.extend(
        _delete_rate_violations(
            decisions,
            notes_by_id,
            max_delete_rate=max_delete_rate,
            max_ghost_delete_rate=max_ghost_delete_rate,
        )
    )
    violations.extend(_overlap_violations(decisions, notes_by_id))
    return violations
