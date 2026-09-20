"""L1: チャンク単位のオーケストレーション(#39, 設計書§7.3/§9)。

`services/score_ops.py`の`apply_ops`/`NoteOp`は使わない(意図的な設計判断、
#39実装時の調査結果):

- `apply_ops`とその内部の`_apply_*`関数群は`Note.provenance = "user"`を
  決め打ちしている(#31: 人間編集専用の関数として設計されている)。
- `NoteUpdateOp`には`spelling`/`ai_reason`/`provenance_run_id`を設定する
  フィールドが無い(L1は文脈判断で選んだ`spelling`をそのまま設定する必要が
  あり、L0のようにmidiから機械的に再計算するのでは表現できない)。
- L1の提案は`current.json`に直接適用せず`score/staging/{run_id}.json`へ
  書くため(設計書§10.3、M4完了条件「L0とL1の差分を確認し小節単位で採否を
  決める」、#41 DiffPanel)、Undo/Redo用のop-logへ個々のdecisionを記録する
  必要が無い(#41が実装する`ai.accept`/`ai.reject`がスコープ単位の粗粒度な
  記録を担う)。

そのため本モジュールは、複製したScoreIRの`Note`オブジェクトを直接ミューテートする
専用ロジックを持つ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from app.domain.invariants import (
    Decision,
    ValidationNote,
    time_occupancies,
    validate_decisions,
)
from app.domain.score import Note, Tie
from app.pipeline.quantize import ticks_to_seconds
from app.pipeline.refine.l1_chunker import ChunkInput, build_chunks
from app.pipeline.refine.l1_client import L1ClientError, call_l1_chunk
from app.pipeline.refine.l1_prompt import build_song_context_message, build_system_prompt
from app.pipeline.refine.voice_assignment import VoiceCandidate, assign_voices
from app.pipeline.time_signature import bar_start_ticks, tick_to_bar_beat, time_signature_at_bar

if TYPE_CHECKING:
    from app.domain.score import ScoreIR

_MIN_DURATION_SEC = 1e-6

# 設計書のDecisionスキーマには#39時点でのmerge_with_previousの対象
# (どの前ノートに統合するか)を指定するフィールドが無く、仕様が未確定のため
# 実装しない(V-2「該当decisionを無視」と同種の扱い、チャンク自体は棄却しない)。
UNSUPPORTED_ACTIONS: Final[frozenset[str]] = frozenset({"merge_with_previous"})
_UNSUPPORTED_ACTIONS = UNSUPPORTED_ACTIONS


@dataclass(frozen=True)
class L1RunResult:
    staged_score: ScoreIR
    chunks_ok: int
    chunks_rejected: int
    # 検証失敗を握り潰さずログ/UIに表示する経路(NFR-06)。
    rejected_reasons: list[str] = field(default_factory=list)
    skipped_decisions: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    )
    # #104: `claude` CLI経由の呼び出しはチャンクごとに実測コスト
    # (`total_cost_usd`)を返すため、モデル単価表からの概算(`cost.py`)ではなく
    # 実際に課金された金額の合計をそのまま持ち回る。
    cost_usd: float = 0.0
    # #109: 検証層が機械的に修復したvoice再割当の内容(NFR-06: 検証・修復の内容を
    # 握り潰さずログ/UIへ伝播させる)。
    voice_repairs: list[str] = field(default_factory=list)


def validation_notes_for_chunk(
    chunk: ChunkInput, notes_by_id: dict[int, Note], decisions: list[Decision]
) -> list[ValidationNote]:
    """`ChunkInput.notes`(L1入力)から検証層向けの`ValidationNote`を組み立てる。

    `onset_beat`/`voice`は、対応する`decision`があれば**適用後**の値を使う
    (#39 Gate2レビュー指摘、2巡目: `keep`の`snap`はonset位置を、
    `voice`/`staff`はvoiceを変更しうるため、適用前の値のままV-8(同一voice内
    時間重複)を検証すると、検証を通過したチャンクでも適用後に重複が生じる
    ケースを見逃す。snapのbeat位置は`ChunkNote.snap_candidates[].beat`に
    既にbeat単位で保持されているため、tick変換をやり直す必要はない)。

    `voice`自体はL1入力スキーマに含まれない(L1が決定する側のフィールドの
    ため)が、V-8には現在のvoiceが必要なため、decisionが無ければ元の`Note`
    から補う(#37 Gate2レビュー指摘: 暗黙keepもV-8の対象に含める)。

    `bar`は`ChunkNote.bar`(小節番号)をそのまま渡す(#67/#68の実測実験で発覚した
    実バグの修正: `ValidationNote.onset_beat`は小節内相対値のため、`bar`が
    無いとV-8が異なる小節のノート同士を誤って重複判定する)。`decision.snap`
    による候補選択がまれに元の小節を跨ぐケース(#38の`_snap_candidates_for_note`
    参照、候補の`beat`は候補自身が属する小節内の値であり得る)は、この関数の
    対象外の既知の限定的な制約として扱う(発生頻度・影響とも小さいと判断)。

    `staff`(#109): `voice`と同じく、明示decisionがあればその値を、無ければ
    ノート自身の(L0由来の)値を使う。L0はstaffごとにvoice番号を1から振るため、
    V-8がvoice番号だけでグルーピングすると、ピアノの大譜表でstaff 1とstaff 2の
    voice 1が互いに重複判定される偽陽性が生じる(実測で確認)。
    """
    decision_by_id = {d.note_id: d for d in decisions}
    result = []
    for chunk_note in chunk.notes:
        decision = decision_by_id.get(chunk_note.id)
        onset_beat = chunk_note.raw_beat
        voice = notes_by_id[chunk_note.id].voice
        staff = notes_by_id[chunk_note.id].staff
        if decision is not None:
            if decision.voice is not None:
                voice = decision.voice
            if decision.staff is not None:
                staff = decision.staff
            if decision.snap is not None:
                candidate = next(
                    (c for c in chunk_note.snap_candidates if c.id == decision.snap), None
                )
                if candidate is not None:
                    onset_beat = candidate.beat
        result.append(
            ValidationNote(
                id=chunk_note.id,
                editable=chunk_note.editable,
                midi=chunk_note.midi,
                bar=chunk_note.bar,
                onset_beat=onset_beat,
                duration_beat=chunk_note.raw_duration_beat,
                voice=voice,
                staff=staff,
                snap_candidate_ids=[c.id for c in chunk_note.snap_candidates],
                flags=chunk_note.flags,
            )
        )
    return result


# 後方互換エイリアス
_validation_notes_for_chunk = validation_notes_for_chunk


def _resolve_split_at_tick(
    decision: Decision,
    *,
    note_bar: int,
    time_signatures: list[dict],
    divisions: int,
) -> int:
    """`decision.split_at_beat`(小節内の拍位置、`ValidationNote.onset_beat`と

    同じ座標系)を絶対tickへ変換する。`domain.invariants`のV-9が同じ座標系で
    範囲チェック済みのため、ここでは変換のみ行う。
    """
    starts = bar_start_ticks(time_signatures, divisions, note_bar)
    numerator, denominator = time_signature_at_bar(time_signatures, note_bar)
    pulse_ticks = divisions * 4 / denominator
    assert decision.split_at_beat is not None  # V-9で保証済み
    return round(starts[note_bar] + (decision.split_at_beat - 1) * pulse_ticks)


def _apply_keep(
    note: Note, decision: Decision, *, run_id: str, beat_anchors: list[tuple[float, float]]
) -> None:
    assert note.onset_tick is not None and note.duration_tick is not None
    if decision.snap is not None:
        candidate = next((c for c in note.snap_candidates if c.id == decision.snap), None)
        if candidate is not None and candidate.tick != note.onset_tick:
            # onset位置が変わる場合のみ再計算する(durationはScore IRの
            # snap_candidatesが候補ごとの値を持たないため不変、#39の既知の制約)。
            note.onset_tick = candidate.tick
            note.onset_sec = ticks_to_seconds(candidate.tick, beat_anchors)
    if decision.spelling is not None:
        note.spelling = decision.spelling
    if decision.voice is not None:
        note.voice = decision.voice
    if decision.staff is not None:
        note.staff = decision.staff
    if decision.tie is not None:
        note.tie = decision.tie
    note.provenance = "llm"
    note.provenance_run_id = run_id
    note.ai_reason = decision.reason


def _apply_delete(note: Note, decision: Decision, *, run_id: str) -> None:
    note.status = "deleted"
    note.provenance = "llm"
    note.provenance_run_id = run_id
    note.ai_reason = decision.reason


def _apply_split_tie(
    score: ScoreIR,
    note: Note,
    decision: Decision,
    *,
    note_bar: int,
    run_id: str,
    time_signatures: list[dict],
    divisions: int,
    beat_anchors: list[tuple[float, float]],
) -> None:
    assert note.onset_tick is not None and note.duration_tick is not None
    at_tick = _resolve_split_at_tick(
        decision, note_bar=note_bar, time_signatures=time_signatures, divisions=divisions
    )
    end_tick = note.onset_tick + note.duration_tick

    new_note = Note(
        id=score.allocate_note_id(),
        onset_sec=0.0,
        duration_sec=_MIN_DURATION_SEC,
        midi=note.midi,
        velocity=note.velocity,
        spelling=decision.spelling if decision.spelling is not None else note.spelling,
        voice=decision.voice if decision.voice is not None else note.voice,
        staff=decision.staff if decision.staff is not None else note.staff,
        tie=Tie(start=True, stop=note.tie.stop),
        provenance="llm",
        provenance_run_id=run_id,
        ai_reason=decision.reason,
    )
    new_note.onset_tick = at_tick
    new_note.duration_tick = end_tick - at_tick
    new_note.onset_sec = ticks_to_seconds(at_tick, beat_anchors)
    new_note.duration_sec = max(
        ticks_to_seconds(end_tick, beat_anchors) - new_note.onset_sec, _MIN_DURATION_SEC
    )

    note.duration_tick = at_tick - note.onset_tick
    note.duration_sec = max(new_note.onset_sec - note.onset_sec, _MIN_DURATION_SEC)
    note.tie = Tie(start=note.tie.start, stop=True)
    note.provenance = "llm"
    note.provenance_run_id = run_id
    note.ai_reason = decision.reason

    part = next(p for p in score.parts if note in p.notes)
    part.notes.append(new_note)


def apply_chunk_decisions(
    decisions: list[Decision],
    *,
    staged: ScoreIR,
    notes_by_id: dict[int, Note],
    run_id: str,
    beat_anchors: list[tuple[float, float]],
    time_signatures_raw: list[dict],
    skipped_decisions: list[str],
) -> None:
    """検証済みの決定リストを対象ノートへ適用する。"""
    for decision in decisions:
        note = notes_by_id.get(decision.note_id)
        if note is None:
            continue  # V-1で棄却対象だが、ここまで来た時点で違反は無い
        if decision.action in UNSUPPORTED_ACTIONS:
            skipped_decisions.append(
                f"note {decision.note_id}: unsupported action {decision.action!r}"
            )
            continue
        note_bar, _ = tick_to_bar_beat(
            note.onset_tick or 0,
            time_signatures=time_signatures_raw,
            divisions=staged.divisions,
        )
        if decision.action == "keep":
            _apply_keep(note, decision, run_id=run_id, beat_anchors=beat_anchors)
        elif decision.action == "delete":
            _apply_delete(note, decision, run_id=run_id)
        elif decision.action == "split_tie":
            _apply_split_tie(
                staged,
                note,
                decision,
                note_bar=note_bar,
                run_id=run_id,
                time_signatures=time_signatures_raw,
                divisions=staged.divisions,
                beat_anchors=beat_anchors,
            )


def repair_voice_conflicts(
    decisions: list[Decision], *, validation_notes: list[ValidationNote]
) -> tuple[list[Decision], dict[int, int], list[str]]:
    """V-8(同一voice内の時間重複)を決定論的に修復する(#109)。

    L1(LLM)が同時発音ノートのvoice分割規則を守らなかった場合、従来はチャンク
    全体を棄却してL0の結果へフォールバックしていた(§9「チャンク棄却」)。その
    結果、1件のvoice重複でチャンク内の他のdecision(異名同音の選択・ghost判定・
    タイ分割等)までまとめて失われていた(#67/#68の実測では違反率5%〜62%)。
    ここでは**voiceだけ**を、L0と同じ区間ベースのアルゴリズム
    (`voice_assignment.assign_voices`)で再割当し、他のdecisionを活かす。

    AIの選択を尊重するため、`preferred`(現在のvoice)を渡して「衝突しない限り
    現状維持」とし、衝突するノートのみを別voiceへ回す(違反ノートのみの最小
    変更、Issue #109の提案どおり)。修復の単位はV-8と同じ`(staff, bar)`
    (`ValidationNote.onset_beat`が小節内相対値のため、小節をまたいで区間比較
    してはならない)。

    Returns:
        `(修復後のdecisions, decisionが無いノート向けのvoice上書き, 修復内容の
        説明リスト)`。修復内容が空(=変更なし)の場合、呼び出し元は従来どおり
        チャンクを棄却する。
    """
    notes_by_id = {n.id: n for n in validation_notes}
    occupancies = time_occupancies(decisions, notes_by_id)
    previous_voice = {occupancy.note_id: occupancy.voice for occupancy in occupancies}

    by_staff_bar: dict[tuple[int, int], list] = {}
    for occupancy in occupancies:
        by_staff_bar.setdefault((occupancy.staff, occupancy.bar), []).append(occupancy)

    repaired_voices: dict[int, int] = {}
    for group in by_staff_bar.values():
        candidates = [
            VoiceCandidate(
                key=occupancy.note_id,
                midi=occupancy.midi,
                onset=occupancy.onset_beat,
                end=occupancy.end_beat,
            )
            for occupancy in group
        ]
        voices, _saturated = assign_voices(
            candidates,
            preferred={occupancy.note_id: occupancy.voice for occupancy in group},
        )
        repaired_voices.update(voices)

    changed = {
        note_id: (previous_voice[note_id], voice)
        for note_id, voice in repaired_voices.items()
        if note_id in previous_voice and previous_voice[note_id] != voice
    }
    if not changed:
        return decisions, {}, []

    decision_ids = {decision.note_id for decision in decisions}
    repairs = [
        f"note {note_id}: voice {previous} -> {repaired}"
        for note_id, (previous, repaired) in sorted(changed.items())
    ]
    voice_overrides = {
        note_id: repaired
        for note_id, (_, repaired) in changed.items()
        if note_id not in decision_ids
    }
    updated_decisions = [
        decision.model_copy(
            update={
                "voice": changed[decision.note_id][1],
                "reason": (
                    f"{decision.reason} / V-8自動修復: voice "
                    f"{changed[decision.note_id][0]} -> {changed[decision.note_id][1]}"
                ),
            }
        )
        if decision.note_id in changed
        else decision
        for decision in decisions
    ]
    return updated_decisions, voice_overrides, repairs


def apply_voice_overrides(
    voice_overrides: dict[int, int], *, notes_by_id: dict[int, Note], run_id: str
) -> None:
    """L1がdecisionを出していない暗黙keepノートのvoiceを修復結果で更新する(#109)。

    これらは`staged`の`Note`を直接更新する(`decision`を持たないため
    `apply_chunk_decisions`では表現できない)。変更の根拠が機械的修復であることを
    `ai_reason`に残し、`provenance`は`llm`(このrunがstagedへ加えた変更)とする。
    """
    for note_id, voice in sorted(voice_overrides.items()):
        note = notes_by_id.get(note_id)
        if note is None or note.voice == voice:
            continue
        previous = note.voice
        note.voice = voice
        note.provenance = "llm"
        note.provenance_run_id = run_id
        note.ai_reason = f"V-8自動修復: voice {previous} -> {voice}(L1のdecision無し)"


def verify_and_apply_chunk_decisions(
    *,
    chunk: ChunkInput,
    decisions: list[Decision],
    staged: ScoreIR,
    part_staves: int,
    notes_by_id: dict[int, Note],
    run_id: str,
    beat_anchors: list[tuple[float, float]],
    time_signatures_raw: list[dict],
    skipped_decisions: list[str],
    voice_repairs: list[str],
) -> tuple[bool, str | None]:
    """チャンクの決定群を検証(V-1〜V-9)し、合格した場合はstagedへ適用する。

    V-8(同一voice内の時間重複)のみが検出された場合は、チャンク全体を棄却せず
    `repair_voice_conflicts`でvoiceを機械的に再割当し、**再検証して通れば適用する**
    (#109)。修復しきれない場合(4声飽和など)やV-8以外の違反が含まれる場合は
    従来どおりチャンク全体を棄却する。

    Returns:
        (ok, rejection_reason): 検証合格時は (True, None)。不合格時は (False, "bars X-Y: reasons")。
    """
    validation_notes = validation_notes_for_chunk(chunk, notes_by_id, decisions)
    violations = validate_decisions(decisions, notes=validation_notes, part_staves=part_staves)

    if violations and all(violation.rule == "V-8" for violation in violations):
        repaired_decisions, voice_overrides, repairs = repair_voice_conflicts(
            decisions, validation_notes=validation_notes
        )
        if repairs:
            revalidated_notes = [
                note.model_copy(update={"voice": voice_overrides.get(note.id, note.voice)})
                for note in validation_notes
            ]
            remaining = validate_decisions(
                repaired_decisions, notes=revalidated_notes, part_staves=part_staves
            )
            if not remaining:
                apply_chunk_decisions(
                    repaired_decisions,
                    staged=staged,
                    notes_by_id=notes_by_id,
                    run_id=run_id,
                    beat_anchors=beat_anchors,
                    time_signatures_raw=time_signatures_raw,
                    skipped_decisions=skipped_decisions,
                )
                apply_voice_overrides(voice_overrides, notes_by_id=notes_by_id, run_id=run_id)
                voice_repairs.extend(
                    f"bars {chunk.context.bars.target} {repair}" for repair in repairs
                )
                return True, None
            reasons = "; ".join(f"{v.rule}(note {v.note_id}): {v.message}" for v in violations)
            return False, (
                f"bars {chunk.context.bars.target}: {reasons} "
                f"(V-8自動修復を試行したが{len(remaining)}件の違反が残る)"
            )

    if violations:
        reasons = "; ".join(f"{v.rule}(note {v.note_id}): {v.message}" for v in violations)
        return False, f"bars {chunk.context.bars.target}: {reasons}"

    apply_chunk_decisions(
        decisions,
        staged=staged,
        notes_by_id=notes_by_id,
        run_id=run_id,
        beat_anchors=beat_anchors,
        time_signatures_raw=time_signatures_raw,
        skipped_decisions=skipped_decisions,
    )
    return True, None


def run_l1_sequential(
    score: ScoreIR,
    part_id: str,
    *,
    run_id: str,
    beat_anchors: list[tuple[float, float]],
    model: str,
    effort: str,
) -> L1RunResult:
    """1パートを対象にL1を逐次モードで実行し、ステージング用ScoreIRを返す。

    `score`自体は変更しない(呼び出し元がdeep copyを渡す前提ではなく、ここで
    複製する)。検証(V-1〜V-9)に1件でも違反すればチャンク全体を棄却し
    (§9「チャンク棄却」)、そのチャンクの対象ノートはL0のまま変更しない。
    """
    if score.find_part(part_id) is None:
        raise ValueError(f"part {part_id!r} not found in score")

    staged = score.model_copy(deep=True)
    part = staged.find_part(part_id)
    assert part is not None
    notes_by_id = {n.id: n for n in part.notes}

    chunks = build_chunks(score, part_id)
    system_prompt = build_system_prompt()
    song_context = build_song_context_message(score, part_id)
    time_signatures_raw = [ts.model_dump(mode="json") for ts in staged.time_signatures]

    chunks_ok = 0
    chunks_rejected = 0
    rejected_reasons: list[str] = []
    skipped_decisions: list[str] = []
    voice_repairs: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost_usd = 0.0

    for chunk in chunks:
        try:
            result = call_l1_chunk(
                chunk,
                system_prompt=system_prompt,
                song_context=song_context,
                model=model,
                effort=effort,
            )
        except L1ClientError as exc:
            chunks_rejected += 1
            rejected_reasons.append(f"bars {chunk.context.bars.target}: {exc}")
            continue

        for key in result.usage:
            usage[key] = usage.get(key, 0) + result.usage[key]
        cost_usd += result.cost_usd

        ok, rejection_reason = verify_and_apply_chunk_decisions(
            chunk=chunk,
            decisions=result.output.decisions,
            staged=staged,
            part_staves=part.staves,
            notes_by_id=notes_by_id,
            run_id=run_id,
            beat_anchors=beat_anchors,
            time_signatures_raw=time_signatures_raw,
            skipped_decisions=skipped_decisions,
            voice_repairs=voice_repairs,
        )
        if ok:
            chunks_ok += 1
        else:
            chunks_rejected += 1
            if rejection_reason:
                rejected_reasons.append(rejection_reason)

    staged.meta.stages["refine"] = {
        "lanes_applied": ["L0", "L1"],
        "l1": {
            "model": model,
            "chunks_ok": chunks_ok,
            "chunks_rejected": chunks_rejected,
            "voice_repair_count": len(voice_repairs),
            "usage": usage,
            "cost_usd": cost_usd,
        },
    }

    return L1RunResult(
        staged_score=staged,
        chunks_ok=chunks_ok,
        chunks_rejected=chunks_rejected,
        rejected_reasons=rejected_reasons,
        skipped_decisions=skipped_decisions,
        voice_repairs=voice_repairs,
        usage=usage,
        cost_usd=cost_usd,
    )
