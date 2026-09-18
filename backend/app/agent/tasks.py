"""標準タスク定義(#49/#50, 設計書§8.7)。

各タスクは TASK.md テンプレート + 許可ツール + 予算のセットとして定義する。
設計書は各タスクの実際のプロンプト文面までは規定していない。#50時点では
`consistency-pass`/`voicing-fix`の2タスクのみ実プロンプト(`prompt_template`)と
ターン/トークン/時間の既定予算を作り込む(issue #50 背景「M5ではこの2つを
実装し、残りはM6で追加する」)。それ以外のタスクは#49時点の目的レベルの
記述のみで、`prompt_template=None`(→`_compose_prompt`が目的文だけの
汎用プロンプトにフォールバックする、`agent_run_manager.py`参照)。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.mcp.sdk_adapter import SERVER_NAME, TOOL_NAMES

# 全タスク共通の既定`allowed_tools`: score-mcpの全8ツール(#43/#44)。タスクごとの
# 絞り込み(例: ghost-sweepにのみBashも許可する)はM6のスコープとし、ここでは
# 「run を起動するとツール呼び出しがリアルタイムに SSE で流れる」を満たす
# 最小限の既定値にとどめる。
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = tuple(f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES)

# `consistency-pass`専用の作業手順。notation_rules.md(全runのワークスペースに
# 既に配置済み、l1_prompt.NOTATION_RULES_MARKDOWNと同一)が異名同音・声部の
# 「規則」自体は既に説明しているため、ここでは重複させず「どのツールをどう
# 使ってscore_validateの違反を自己修正するか」という手順に絞る。
#
# 重要な制約: score_apply_ops(services/score_ops.py)には現時点でスペリング
# (異名同音表記)を直接変更するNoteOpが無い — note.updateでmidiを変えずに
# spellingだけを付け替える手段が無い(#50調査で判明)。そのため異名同音の
# 見直しは「発見・報告のみ」とし、実際に自己修正できるのはvoice/staffの
# 割り当てとV-8(同一voice内の時間重複)に限定する — これはscore_validateが
# 実際に検証する内容とも一致する(_lint_scoreがチェックするのはV-8と
# 削除率V-6のみで、V-4/V-5のスペリング系は現状評価対象外のため)。
_CONSISTENCY_PASS_PROMPT_TEMPLATE = """\
consistency-pass専用の作業手順:

1. mcp__score__score_context で調・拍子・パート編成を把握する。
2. 各パートについて mcp__score__score_validate(scope={"part_id": <part_id>}) \
を呼び、現状の検証違反(主にV-8: 同一voice内の時間重複)を確認する。
3. mcp__score__score_stats(part=<part_id>, metric="onset"|"pitch_range"|\
"chord_density"|"velocity") で声部の分布・密度を把握し、notation_rules.md の\
「声部・大譜表の割り当て」規則に沿っているか、mcp__score__score_query で\
実際のノートを確認する。
4. 規則から外れている、またはscore_validateが違反を報告した箇所を見つけたら、\
mcp__score__score_apply_ops(ops=[{"type": "note.update", "note_ids": [...], \
"voice": <1-4>, "staff": <1..part.staves>}]) で修正する。
5. 修正後、再度 score_validate を呼び、新たな違反が発生していないことを確認する。\
score_apply_ops自体も適用前後で自動検証しており、新たな違反が生じるopsは\
{"ok": false, "violations": [...]}を返して書き込みを拒否する — その場合はops\
を見直して再試行する。
6. 全パートを一巡したら、修正できなかった課題があれば report.md に明記する。\
異名同音(スペリング)の見直しが必要な箇所を見つけても、score_apply_opsには\
現時点でスペリングのみを変更する手段が無いため修正はできない — 発見した箇所を\
report.md に記録するにとどめること(直接修正しようとしてmidiを変更すると\
音高そのものが変わってしまうため、絶対に行わないこと)。
"""

_VOICING_FIX_PROMPT_TEMPLATE = """\
voicing-fix専用の作業手順:

1. 対象範囲(TASK.mdのスコープに記載されたpart/小節範囲)を \
mcp__score__score_query(part=<part_id>, bars=[<開始小節>, <終了小節>]) で取得する。
2. notation_rules.md の「声部・大譜表の割り当て」規則(ピアノの場合: MIDI>=60は\
staff1、MIDI<60はstaff2、同時発音は音高が高い順にvoice1,2,3,4、旋律的な\
フレーズは頻繁にvoiceを切り替えない)に照らし、voice/staffの割り当てが\
適切か確認する。
3. 問題があれば mcp__score__score_apply_ops(ops=[{"type": "note.update", \
"note_ids": [...], "voice": <1-4>, "staff": <1..part.staves>}]) で修正する。
4. mcp__score__score_validate(scope={"part_id": <part_id>}) で新たな\
時間重複(V-8)が発生していないことを確認する。
5. 修正内容(どのノートのvoice/staffをどう変えたか、その理由)を report.md に\
まとめる。
"""


@dataclass(frozen=True)
class TaskDefinition:
    """1タスク定義。`tools`は設計書§8.7の表の「主に使うツール」列(表示用)であり、

    実際にプロバイダへ渡す`allowed_tools`(mcp__score__*形式)とは別物。
    `max_tokens_budget`/`timeout_sec`が`None`のタスクは`AgentTask`自身の既定値
    (300,000トークン/900秒)を使う — `turns_max`(必須)ほど差がつかない
    タスクでは無理に上書きしない。
    """

    id: str
    purpose: str
    tools: tuple[str, ...]
    turns_min: int | None
    turns_max: int
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    max_tokens_budget: int | None = None
    timeout_sec: int | None = None
    prompt_template: str | None = None


STANDARD_TASKS: tuple[TaskDefinition, ...] = (
    TaskDefinition(
        id="refine-part",
        purpose="1パートを通しで整音(L1の代替として使う場合)",
        tools=("query", "apply_ops", "validate"),
        turns_min=30,
        turns_max=60,
    ),
    TaskDefinition(
        id="consistency-pass",
        purpose="曲全体で声部・異名同音・記譜の一貫性を担保",
        tools=("query", "stats", "apply_ops", "validate"),
        turns_min=20,
        turns_max=40,
        # 曲全体を全パート走査するため既定(300k/900s)より広めの予算を取る。
        max_tokens_budget=400_000,
        timeout_sec=1200,
        prompt_template=_CONSISTENCY_PASS_PROMPT_TEMPLATE,
    ),
    TaskDefinition(
        id="repeat-alignment",
        purpose="繰り返し区間を検出し記譜を揃える",
        tools=("query", "stats", "apply_ops"),
        turns_min=15,
        turns_max=30,
    ),
    TaskDefinition(
        id="ghost-sweep",
        purpose="統計からこの曲固有のゴースト閾値を決め適用",
        tools=("stats", "Bash(python)", "apply_ops"),
        turns_min=10,
        turns_max=25,
    ),
    TaskDefinition(
        id="voicing-fix",
        purpose="指定範囲の声部/大譜表割り当てを修正",
        tools=("query", "apply_ops", "validate"),
        turns_min=10,
        turns_max=20,
        # 単一スコープ(part/小節範囲)に限定されるため既定より小さめでよい。
        max_tokens_budget=200_000,
        timeout_sec=600,
        prompt_template=_VOICING_FIX_PROMPT_TEMPLATE,
    ),
    TaskDefinition(
        id="export-qa",
        purpose="MusicXMLを生成し記譜上の問題を検出・修正",
        tools=("render", "apply_ops", "validate"),
        turns_min=15,
        turns_max=30,
    ),
    TaskDefinition(
        id="investigate",
        purpose="自然言語の自由指示(UC-4/FR-21)",
        tools=("全ツール",),
        turns_min=None,
        turns_max=40,
    ),
)

_BY_ID: dict[str, TaskDefinition] = {t.id: t for t in STANDARD_TASKS}


def get_task_definition(task_type: str) -> TaskDefinition | None:
    return _BY_ID.get(task_type)
