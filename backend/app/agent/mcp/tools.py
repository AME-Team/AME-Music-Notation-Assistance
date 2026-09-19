"""score-mcp コア実装(#43, 設計書§8.4)— Score IR への唯一の書き込み経路。

エージェントはこのモジュールの関数を呼ぶことでしか Score IR を変更できない
(§8.4「エージェントが賢いことに安全性を依存させず、構造として担保する」)。
各関数は純粋な Python 関数であり、Claude Agent SDK にも `mcp` パッケージにも
依存しない — SDK依存の薄いラッパー(インプロセス/stdio)は#44が別ファイルとして
書く(§8.4「実装形態 — コアは1つ、アダプタは2つ」)。

書き込みを伴うのは`score_apply_ops`だけであり、その書き込み先は
`score/current.json`ではなく`score/staging/{run_id}.json`(#39/#41のL1と同じ
機構)。これにより既存の`pipeline/refine/l1_diff.py`(provenance_run_idにのみ
着目しL1/L2を区別しない)と`api/diff.py`の承認/却下エンドポイントを無改修の
ままL2にも再利用できる(承認フロー自体の担当は#46)。

戻り値は`Note`/`ScoreIR`を直接返さず、`model_dump(mode="json")`した素の
dict/listのみを返す — #44の2つのアダプタがSDK型を経由せずそのままMCP
レスポンスへ渡せるようにするため。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from app.domain.invariants import Decision, ValidationNote, Violation, validate_decisions
from app.domain.migrations import migrate_to_current
from app.domain.score import Note, ScoreIR
from app.domain.score_ops import NoteOp
from app.infra import storage
from app.pipeline.export.musicxml import ExportError, render_musicxml
from app.pipeline.quantize import beat_tick_anchors
from app.pipeline.time_signature import tick_to_bar_beat, time_signature_at_bar
from app.services.score_ops import ScoreOpError
from app.services.score_ops import apply_ops as _apply_ops
from app.services.score_service import ScoreService

_ops_adapter: TypeAdapter[list[NoteOp]] = TypeAdapter(list[NoteOp])


@dataclass(frozen=True)
class ToolContext:
    """1エージェントrunにつき1つ。#44のアダプタがサーバ構築時に1回作る。"""

    workspace_dir: Path
    project_id: str
    run_id: str


class ToolError(RuntimeError):
    """score-mcpツール共通のドメインエラー。#44のアダプタがMCPエラー応答へ変換する。

    `score_apply_ops`が返す構造化された`{"ok": False, "violations": [...]}`とは
    区別する: こちらは「エージェントがopsを直しても解決しない」種類の失敗
    (対象part/noteが存在しない、beatmap未実行等の前提条件エラー)に使う。
    """


# ---------------------------------------------------------------------------
# 内部ヘルパ: staging file 初期化・読み書き
# ---------------------------------------------------------------------------


def _load_working_score(ctx: ToolContext) -> ScoreIR:
    """このrunのstagingファイルがあればそれを、無ければcurrent.jsonを種にする。

    「run内の最初の`score_apply_ops`呼び出しでcurrent.jsonから初期化し、以後は
    stagingファイルを読み継ぐ」という要件をここに集約する。読み取り専用ツール
    (`score_query`等)もこの関数を使うことで、エージェントが自分のrun内での
    直前の編集を踏まえて次の判断ができる。

    毎回ディスクから新規にパースするため、戻り値は他のどこからも参照されて
    いない独立したオブジェクトであることが保証される(呼び出し元がそのまま
    ミューテートしてよい、deep copy不要)。

    stagingファイル破損時(不正なJSON/スキーマ不一致)を含め、失敗は全て
    `ToolError`へ変換する(#43 Gate2レビュー指摘・2巡目: 以前はcurrent.json
    読み込み側のみ`try`で包んでおり、stagingファイル側の`read_json`/
    `migrate_to_current`/`ScoreIR.model_validate`の失敗が生の例外として
    伝播していた)。
    """
    staging_path = storage.score_staging_path(ctx.workspace_dir, ctx.project_id, ctx.run_id)
    try:
        if staging_path.exists():
            raw = storage.read_json(staging_path)
            return ScoreIR.model_validate(migrate_to_current(raw))
        return ScoreService(workspace_dir=ctx.workspace_dir).read_score(ctx.project_id)
    except Exception as exc:  # noqa: BLE001 - あらゆる読み込み失敗をToolErrorへ一元変換
        raise ToolError(f"failed to read score for project {ctx.project_id!r}: {exc}") from exc


def _write_staging(ctx: ToolContext, score: ScoreIR) -> None:
    storage.write_json(
        storage.score_staging_path(ctx.workspace_dir, ctx.project_id, ctx.run_id),
        score.model_dump(mode="json"),
    )


def _load_beat_anchors(ctx: ToolContext, divisions: int) -> list[tuple[float, float]]:
    """`api/score.py::apply_score_ops`と同じfail-closedパターン。

    beatmap欠落/ビート情報が空の場合、無言でtick→秒変換を続けず`ToolError`とする。
    beatmap.json自体が破損している場合(不正なJSON等)も同様に`ToolError`へ
    変換する(#43 Gate2レビュー指摘・2巡目)。
    """
    beatmap_file = storage.beatmap_path(ctx.workspace_dir, ctx.project_id)
    if not beatmap_file.exists():
        raise ToolError("beatmap not found; run the beat stage first")
    try:
        beatmap = storage.read_json(beatmap_file)
        anchors = beat_tick_anchors(
            beatmap.get("beats", []), beatmap.get("time_signatures", []), divisions
        )
    except Exception as exc:  # noqa: BLE001 - JSONDecodeError等をToolErrorへ一元変換
        raise ToolError(f"failed to read beatmap for project {ctx.project_id!r}: {exc}") from exc
    if not anchors:
        raise ToolError("beatmap has no beats; cannot convert tick positions to seconds")
    return anchors


def _duration_beat(
    note: Note, *, time_signatures: list[dict[str, Any]], divisions: int, bar: int
) -> float:
    """`pipeline/refine/l1_chunker.py::_duration_beats`と同じ換算(拍子分母基準の

    pulse)。`ValidationNote.onset_beat`/`duration_beat`と単位を揃えるために
    複製する(l1_chunker.py側はモジュール非公開のヘルパのため、ここでは
    独立して計算する)。
    """
    _, denominator = time_signature_at_bar(time_signatures, bar)
    pulse_ticks = divisions * 4 / denominator
    return (note.duration_tick or 0) / pulse_ticks


# ---------------------------------------------------------------------------
# 共有ヘルパ: 現在のノート状態をV-6/V-8で検証する("lint")
# ---------------------------------------------------------------------------


def _lint_score(
    score: ScoreIR, *, part_id: str | None = None, run_id: str | None = None
) -> list[Violation]:
    """スコアの現状をV-6(このrunが新規に削除した比率)・V-8(同一voice内の

    時間重複)で検証する(`domain/invariants.py::validate_decisions`、#37を再利用)。

    `validate_decisions`はL1のDecision語彙(spelling/snap等を明示的に再提案する
    前提)向けであり、「既存データをそのまま検証する」用途には素直に嵌らない。
    特にV-3(snap候補チェック)は`action`が"keep"/"split_tie"のdecisionに対し
    snapの明示指定を無条件に要求するため、「何も変更を提案していない」ことを
    表すために`snap=None`の恒等decisionを合成すると常に(誤って)V-3が発火する。
    そのため本関数は**個々のノードに対してdecisionを合成しない**設計にする:
    `notes`には対象ノートをそのまま渡し、`decisions`は空(または後述のdelete分
    のみ)にする。`validate_decisions`の「decisionが無いノートは暗黙keepとして
    扱う」パス(V-10)により、V-8はこの経路でも正しく機能する一方、
    decision前提のV-1〜V-5/V-7(decision側)/V-9は評価自体が発生しない
    (=誤検知しない)。

    V-6(delete率)はdecisionsが空だと常に0%になり検出できない。`run_id`を
    指定した場合のみ、`status=="deleted"`かつ`provenance_run_id==run_id`の
    ノート(=このrunが今まさに削除したノート)に`action="delete"`のdecisionを
    明示的に合成し、「このrunの累積delete率」としてV-6を機能させる
    (`score_apply_ops`の事後lintから使う想定)。`score_validate`(run跨ぎの
    現状確認)からは`run_id`未指定で呼び、V-8のみを実効チェックとする。

    それ以外の状態のノート("muted"、または他run/過去に削除済みのノート)は
    `notes`から除外する — `ValidationNote`に`status`フィールドが無く、
    含めると誤って「時間を占有する現役ノート」としてV-8に数えられて
    しまうため。
    """
    ts_dicts = [ts.model_dump(mode="json") for ts in score.time_signatures]
    violations: list[Violation] = []
    for part in score.parts:
        if part_id is not None and part.id != part_id:
            continue
        decisions: list[Decision] = []
        notes: list[ValidationNote] = []
        for note in part.notes:
            if note.onset_tick is None:
                continue
            is_deleted_by_this_run = (
                run_id is not None and note.status == "deleted" and note.provenance_run_id == run_id
            )
            if note.status == "muted" or (note.status == "deleted" and not is_deleted_by_this_run):
                continue

            bar, onset_beat = tick_to_bar_beat(
                note.onset_tick, time_signatures=ts_dicts, divisions=score.divisions
            )
            duration_beat = _duration_beat(
                note, time_signatures=ts_dicts, divisions=score.divisions, bar=bar
            )
            notes.append(
                ValidationNote(
                    id=note.id,
                    editable=True,
                    midi=note.midi,
                    bar=bar,
                    onset_beat=onset_beat,
                    duration_beat=duration_beat,
                    voice=note.voice,
                    snap_candidate_ids=[c.id for c in note.snap_candidates],
                    flags=note.flags,
                )
            )
            if is_deleted_by_this_run:
                decisions.append(
                    Decision(note_id=note.id, action="delete", reason="lint: deleted by this run")
                )
        violations.extend(validate_decisions(decisions, notes=notes, part_staves=part.staves))
    return violations


def _violations_to_dicts(violations: list[Violation]) -> list[dict[str, Any]]:
    return [dataclasses.asdict(v) for v in violations]


# ---------------------------------------------------------------------------
# 読み取り専用ツール(副作用なし)
# ---------------------------------------------------------------------------


def _notes_in_scope(
    score: ScoreIR, *, part_id: str, bars: tuple[int, int] | None
) -> list[tuple[Note, int, float]]:
    """指定part内のactiveノートを`(note, bar, beat)`で返す(`bars`指定時は範囲内のみ)。"""
    part = score.find_part(part_id)
    if part is None:
        raise ToolError(f"part not found: {part_id!r}")
    ts_dicts = [ts.model_dump(mode="json") for ts in score.time_signatures]
    result: list[tuple[Note, int, float]] = []
    for note in part.notes:
        if note.status != "active" or note.onset_tick is None:
            continue
        bar, beat = tick_to_bar_beat(
            note.onset_tick, time_signatures=ts_dicts, divisions=score.divisions
        )
        if bars is not None and not (bars[0] <= bar <= bars[1]):
            continue
        result.append((note, bar, beat))
    return result


def _matches_filter(note: Note, filter_: dict[str, Any]) -> bool:
    """`score_query`の`filter`引数の素朴な等値/所属フィルタ(#43の意図的な最小実装)。

    `flags_contains`だけ特別扱いし(`note.flags`はlistのため等値比較が無意味)、
    それ以外のキーは`Note`の同名フィールドとの単純な等値比較とする。
    """
    for key, value in filter_.items():
        if key == "flags_contains":
            if value not in note.flags:
                return False
            continue
        if getattr(note, key, None) != value:
            return False
    return True


def score_query(
    ctx: ToolContext,
    *,
    part: str,
    bars: tuple[int, int],
    filter: dict[str, Any] | None = None,  # noqa: A002 - §8.4のツール引数名に合わせる
) -> list[dict[str, Any]]:
    """指定part・小節範囲のノート配列(snap候補・flags含む)を返す。副作用なし。"""
    score = _load_working_score(ctx)
    results: list[dict[str, Any]] = []
    for note, bar, beat in _notes_in_scope(score, part_id=part, bars=bars):
        if filter is not None and not _matches_filter(note, filter):
            continue
        data = note.model_dump(mode="json")
        data["bar"] = bar
        data["beat"] = beat
        results.append(data)
    return results


def score_context(ctx: ToolContext) -> dict[str, Any]:
    """調・拍子・テンポマップ・コード進行をまとめて返す。副作用なし。"""
    score = _load_working_score(ctx)
    chords = score.chords
    if not chords:
        from app.pipeline.harmony import estimate_chords_for_score

        chords = estimate_chords_for_score(score)
    return {
        "key_signatures": [k.model_dump(mode="json") for k in score.key_signatures],
        "time_signatures": [t.model_dump(mode="json") for t in score.time_signatures],
        "tempo_map": [t.model_dump(mode="json") for t in score.tempo_map],
        "chords": [c.model_dump(mode="json") for c in chords],
    }


def score_stats(
    ctx: ToolContext,
    *,
    part: str,
    metric: Literal["onset", "pitch_range", "chord_density", "velocity"],
    bars: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """onset分布・音域・和音密度・ベロシティ分布の軽量統計。副作用なし。

    既存コードに統計計算のヘルパは無いため新規実装する(#43)。設計書は各
    metricの厳密な出力形状まで規定していないため、エージェントが判断材料に
    使える最小限の集計にとどめる。
    """
    score = _load_working_score(ctx)
    scoped = _notes_in_scope(score, part_id=part, bars=bars)
    notes = [n for n, _, _ in scoped]

    if metric == "onset":
        by_bar: dict[int, int] = {}
        for _, bar, _ in scoped:
            by_bar[bar] = by_bar.get(bar, 0) + 1
        return {"count": len(notes), "by_bar": by_bar}
    if metric == "pitch_range":
        if not notes:
            return {"min_midi": None, "max_midi": None, "mean_midi": None}
        midis = [n.midi for n in notes]
        return {
            "min_midi": min(midis),
            "max_midi": max(midis),
            "mean_midi": sum(midis) / len(midis),
        }
    if metric == "velocity":
        if not notes:
            return {"min": None, "max": None, "mean": None}
        velocities = [n.velocity for n in notes]
        return {
            "min": min(velocities),
            "max": max(velocities),
            "mean": sum(velocities) / len(velocities),
        }
    if metric == "chord_density":
        if bars is not None:
            lo, hi = bars
        else:
            observed_bars = [bar for _, bar, _ in scoped]
            lo, hi = 1, max(observed_bars, default=1)
        num_bars = max(hi - lo + 1, 1)
        chords_in_range = [c for c in score.chords if lo <= c.bar <= hi]
        return {"count": len(chords_in_range), "chords_per_bar": len(chords_in_range) / num_bars}
    raise ToolError(f"unknown metric: {metric!r}")


def score_validate(
    ctx: ToolContext, *, scope: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """スコアの現状をV-8(同一voice内の時間重複)で検証する(`_lint_score`参照)。

    `scope`は`{"part_id": "..."}`のみ対応(#43の意図的な最小実装)。`run_id`は
    渡さないため、V-6(delete率)はここでは評価されない(常に0件)— この関数は
    「過去の編集履歴に関わらず今のスコアはおかしくないか」を見るためのもので、
    「このrunが提案した変更の是非」はscore_apply_opsの事後lintが別途担う。
    """
    score = _load_working_score(ctx)
    part_id = (scope or {}).get("part_id")
    return _violations_to_dicts(_lint_score(score, part_id=part_id))


def score_render(
    ctx: ToolContext,
    *,
    scope: dict[str, Any] | None,
    format: Literal["musicxml", "stats"],  # noqa: A002
) -> Any:
    """MusicXML文字列、または軽量な記譜統計を返す。副作用なし。

    `format="musicxml"`は`scope`を無視し常に全体を返す(既存の
    `pipeline.export.musicxml.render_musicxml`が部分スコアのレンダリングを
    サポートしないため、#43のスコープ外として別issueに委ねる)。
    `format="stats"`は`scope={"part_id": ...}`があればそのpartのみに絞る。
    """
    score = _load_working_score(ctx)
    if format == "musicxml":
        try:
            xml_bytes = render_musicxml(score.model_dump(mode="json"))
        except ExportError as exc:
            raise ToolError(str(exc)) from exc
        return xml_bytes.decode("utf-8")
    if format == "stats":
        part_id = (scope or {}).get("part_id")
        parts = [p for p in score.parts if part_id is None or p.id == part_id]
        ts_dicts = [ts.model_dump(mode="json") for ts in score.time_signatures]
        max_bar = 1
        note_count = 0
        for part in parts:
            for note in part.notes:
                if note.status != "active":
                    continue
                note_count += 1
                if note.onset_tick is not None:
                    bar, _ = tick_to_bar_beat(
                        note.onset_tick, time_signatures=ts_dicts, divisions=score.divisions
                    )
                    max_bar = max(max_bar, bar)
        return {"part_count": len(parts), "note_count": note_count, "bar_count": max_bar}
    raise ToolError(f"unknown format: {format!r}")


def baseline_diff(ctx: ToolContext, *, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """L0ベースラインとの差分。#43時点では未実装。

    L0のスナップショット(quantizeステージ完了直後の状態)はどこにも永続化
    されておらず(`worker/dsp_main.py`が`score/current.json`へ直接上書きし、
    後続ステージ/L1/L2が触れると失われる)、実装する材料が無い
    (ユーザー確認済み、#43では別issueへ先送り)。
    """
    raise ToolError(
        "baseline_diff is not implemented: no persisted L0 baseline snapshot exists yet "
        "(requires a follow-up issue to persist score/baseline.json at end of the quantize stage)"
    )


def score_note_history(ctx: ToolContext, *, note_id: int) -> list[dict[str, Any]]:
    """指定ノートの変更履歴。`score/ops.jsonl`(監査ログ、#32)を読み、

    `changes`に対象note_idを含む行だけ抽出して返す。`storage.py`はjsonlの
    追記(`append_jsonl`)のみを提供し読み取りヘルパが無いため、この関数専用に
    軽量な読み取りをここへ実装する。

    壊れた行(不正なJSON、`changes`が非dict等)に遭遇しても例外を伝播させず
    `ToolError`へ変換する(#43 Gate2レビュー指摘: 読み取り専用ツールが
    `score/ops.jsonl`の1行の破損だけで丸ごと落ちるのは、このモジュールの
    「環境側の失敗はToolErrorで伝える」という契約に反する)。
    """
    ops_log_path = storage.score_ops_log_path(ctx.workspace_dir, ctx.project_id)
    if not ops_log_path.exists():
        return []
    key = str(note_id)
    history: list[dict[str, Any]] = []
    try:
        with ops_log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    continue
                changes = entry.get("changes")
                if not isinstance(changes, dict):
                    continue
                change = changes.get(key)
                if change is not None:
                    history.append(
                        {"actor": entry.get("actor"), "ts": entry.get("ts"), "change": change}
                    )
    except json.JSONDecodeError as exc:
        raise ToolError(f"score/ops.jsonl contains malformed JSON: {exc}") from exc
    return history


# ---------------------------------------------------------------------------
# 唯一の書き込みツール
# ---------------------------------------------------------------------------


def score_apply_ops(ctx: ToolContext, *, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """§8.4「唯一の書き込み経路」の心臓部。

    1. `ops`(生dict配列、MCP呼び出し経由でJSONとして届く想定)を`NoteOp`へ
       parseする。不正な形の場合は書き込まず構造化した違反として返す。
    2. working score(このrunのstaging、無ければcurrent.jsonから初期化)へ
       opsを適用する前の状態で`_lint_score`を実行し、既存の(このopsとは無関係な)
       違反を`pre_violations`として記録しておく。AMT/quantize直後の
       `current.json`には、このrunが一切関与していないV-8違反(同一voice内
       重複)が既に残っている場合があり、それを理由に無関係な編集まで
       ブロックしてしまうと、エージェントが一切の編集をできなくなる
       (#43 Gate2レビュー指摘)。
    3. working scoreへ`services.score_ops.apply_ops`を`provenance="agent"`,
       `provenance_run_id=ctx.run_id`で適用する。`ScoreOpError`(不正な
       note_id/範囲外の値等)は捕捉し、何も書かずに構造化した違反を返す
       (FR-23のトランザクション性: 1つでも失敗すれば全体を適用しない)。
    4. 成功したら`_lint_score`で事後検証し、`pre_violations`に無かった
       **新規の**違反(V-6: このrunの累積delete率、V-8: 同一voice内の時間
       重複)だけを見る。新たな違反があれば、何も書かず違反を返す。
    5. 新規違反ゼロなら`score/staging/{run_id}.json`へ書き込み、成功を返す
       (既存の無関係な違反が残っていても、このopsが悪化させていなければ許可する)。

    `_load_working_score`/`_load_beat_anchors`起因の`ToolError`(beatmap未実行
    等、opsを直しても解決しない前提条件エラー)はここでは捕捉せずそのまま
    呼び出し元(#44のアダプタ)へ伝播させる — `{"ok": False, ...}`という
    「エージェントが自己修正できる」応答と、例外という「環境側の問題」を
    区別するため。
    """
    try:
        parsed_ops = _ops_adapter.validate_python(ops)
    except ValidationError as exc:
        return {
            "ok": False,
            "violations": [{"rule": "SCORE_OP_ERROR", "note_id": None, "message": str(exc)}],
        }

    working = _load_working_score(ctx)
    anchors = _load_beat_anchors(ctx, working.divisions)
    pre_violations = set(_lint_score(working, run_id=ctx.run_id))

    try:
        _apply_ops(working, parsed_ops, anchors, provenance="agent", provenance_run_id=ctx.run_id)
    except ScoreOpError as exc:
        return {
            "ok": False,
            "violations": [{"rule": "SCORE_OP_ERROR", "note_id": None, "message": str(exc)}],
        }

    post_violations = _lint_score(working, run_id=ctx.run_id)
    new_violations = [v for v in post_violations if v not in pre_violations]
    if new_violations:
        return {"ok": False, "violations": _violations_to_dicts(new_violations)}

    _write_staging(ctx, working)
    return {"ok": True, "violations": [], "run_id": ctx.run_id}
