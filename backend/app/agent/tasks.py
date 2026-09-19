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
#
# Gate2レビュー指摘(HIGH、#50): この一覧にRead/Write/Bashが含まれていないため
# 「エージェントがTASK.md/notation_rules.mdを読めず、report.mdも書けないのでは
# ないか」という懸念が示されたが、実機テスト(#50、認証済みClaude Code CLI経由で
# consistency-passを実行)で否定された — 実際にRead/Write/Bashの呼び出しが
# audit.jsonlに記録され、成功している。これは`claude.py::_build_options`が
# `permission_mode="bypassPermissions"`を設定しており(全許可チェックを迂回)、
# かつ`ClaudeAgentOptions.tools`を明示的に絞り込んでいない(既定の全ビルトイン
# ツールセットが有効なまま)ため — `allowed_tools`はMCPツールの自動承認/発見
# 対象を絞るだけで、Read/Write/Bash等のビルトインツールの可否には影響しない
# (実際の権限強制は`agent/policy.py`のPreToolUseフックが構造的に担う、#45)。
# 将来SDKの既定挙動が変わった場合に備え、この前提が崩れていないかは
# 実機テストで確認すること(単体テストはSDKをモックするため検出できない)。
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

_REPEAT_ALIGNMENT_PROMPT_TEMPLATE = """\
repeat-alignment専用の作業手順:

1. mcp__score__score_context で曲の全体構造(調・拍子・テンポ)を把握する。
2. mcp__score__score_stats(part=<part_id>, metric="onset") や mcp__score__score_query \
を用いて、曲中で同一または類似のリズムパターン・フレーズが繰り返されている\
小節区間(Aメロの1回目と2回目、サビ等)を検出する。
3. 検出した繰り返し区間同士の記譜(声部割り当て、異名同音表記の整合、タイの扱い)を比較する。
4. 意図しない表記の揺らぎや声部割り当ての不一致があれば、mcp__score__score_apply_ops で\
一方の記譜スタイルに統一する。
5. 検出した繰り返し区間の一覧と、揃えた記譜の内容を report.md にまとめる。
"""

_GHOST_SWEEP_PROMPT_TEMPLATE = """\
ghost-sweep専用の作業手順:

1. 対象パート(または全パート)について mcp__score__score_stats(part=<part_id>, metric="velocity") \
および metric="pitch_range" でベロシティ分布・音域を把握する。
2. ベロシティのヒストグラムや統計情報から、この楽曲の演奏ノイズフロアおよび\
ゴーストノート(意図しない微弱音・極短音)の閾値を決定する。
3. 必要に応じて Bash 環境で Python スクリプトを実行し、より詳細な統計や\
クラスタリング分析を行って閾値の妥当性を確認する。
4. ゴーストノート候補を mcp__score__score_query で確認し、不適切なノートに対して\
mcp__score__score_apply_ops(ops=[{"type": "note.update", "note_ids": [...], \
"flags": ["ghost_candidate"]}]) または適切な修正を適用する。
5. 判定基準と適用結果を report.md に詳細に記録する。
"""

_EXPORT_QA_PROMPT_TEMPLATE = """\
export-qa専用の作業手順:

1. 各パートについて mcp__score__score_render(scope={"part_id": <part_id>}, format="musicxml") \
を実行し、MusicXML の生成を試行する。
2. 出力された MusicXML 文字列や生成時の警告・エラーを確認し、\
記譜上の不整合(小節内の拍数の過不足、不正なタイの接続、音域外記譜など)を特定する。
3. mcp__score__score_validate(scope={"part_id": <part_id>}) も併せて実行し、検証違反を確認する。
4. 発見した記譜上の問題を mcp__score__score_apply_ops で修正する。
5. 修正後に再度 render と validate を実行し、問題が解消されたことを確認する。
6. 検出された問題点と修正内容、最終的な MusicXML の検証状態を report.md に報告する。
"""

_INVESTIGATE_PROMPT_TEMPLATE = """\
investigate専用の作業手順:

1. TASK.md に記載されたユーザーの調査・修正依頼の指示内容を注意深く確認する。
2. 必要に応じて mcp__score__* の各ツール(context, query, stats, validate, render, apply_ops) \
やワークスペース内のファイルを活用して調査を進める。
3. スコアの修正を伴う指示の場合は、mcp__score__score_apply_ops を通じて変更を適用し、\
適用後に必ず mcp__score__score_validate でスコアの健全性を検証する。
4. 調査結果・修正内容・ユーザーへの回答を report.md にわかりやすく整理して報告する。
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
    # Gate2レビュー指摘(MIDDLE): voicing-fixのように「対象範囲」が作業の前提
    # そのものであるタスクで、呼び出し元が`scope`を指定し忘れた場合にサイレントに
    # (スコープ抜きの不完全な指示のまま)起動して予算を浪費するのを防ぐ。
    # `AgentRunManager.create_run`が`investigate`の`MissingPromptError`と同じ
    # パターンでfail-fastする。
    requires_scope: bool = False


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
        # Gate2レビュー指摘・LOW: 実プロンプト(_CONSISTENCY_PASS_PROMPT_TEMPLATE)は
        # score_contextの使用も指示しているため、表示用のtools一覧にも含める
        # (§8.7の元表には無いが、実手順との整合を優先する)。
        tools=("context", "query", "stats", "apply_ops", "validate"),
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
        tools=("context", "query", "stats", "apply_ops", "validate"),
        turns_min=15,
        turns_max=30,
        max_tokens_budget=300_000,
        timeout_sec=900,
        prompt_template=_REPEAT_ALIGNMENT_PROMPT_TEMPLATE,
    ),
    TaskDefinition(
        id="ghost-sweep",
        purpose="統計からこの曲固有のゴースト閾値を決め適用",
        tools=("query", "stats", "Bash(python)", "apply_ops"),
        turns_min=10,
        turns_max=25,
        max_tokens_budget=250_000,
        timeout_sec=750,
        prompt_template=_GHOST_SWEEP_PROMPT_TEMPLATE,
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
        requires_scope=True,
    ),
    TaskDefinition(
        id="export-qa",
        purpose="MusicXMLを生成し記譜上の問題を検出・修正",
        tools=("render", "apply_ops", "validate"),
        turns_min=15,
        turns_max=30,
        max_tokens_budget=300_000,
        timeout_sec=900,
        prompt_template=_EXPORT_QA_PROMPT_TEMPLATE,
    ),
    TaskDefinition(
        id="investigate",
        purpose="自然言語の自由指示(UC-4/FR-21)",
        tools=("全ツール",),
        turns_min=None,
        turns_max=40,
        max_tokens_budget=400_000,
        timeout_sec=1200,
        prompt_template=_INVESTIGATE_PROMPT_TEMPLATE,
    ),
)

_BY_ID: dict[str, TaskDefinition] = {t.id: t for t in STANDARD_TASKS}


def get_task_definition(task_type: str) -> TaskDefinition | None:
    return _BY_ID.get(task_type)
