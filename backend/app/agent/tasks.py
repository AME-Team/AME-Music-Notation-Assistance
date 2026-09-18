"""標準タスク定義(#49, 設計書§8.7)。

各タスクは TASK.md テンプレート + 許可ツール + 予算のセットとして定義する。
設計書は各タスクの実際のプロンプト文面までは規定していない —
`consistency-pass`/`voicing-fix`の実プロンプトの作り込みは#50の担当。ここでは
`GET /api/agent/tasks`が返す一覧、および`AgentRunManager`がrun起動時に使う
`max_turns`/`allowed_tools`の既定値を定義する(#49完了条件を満たす最小限)。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.mcp.sdk_adapter import SERVER_NAME, TOOL_NAMES

# 全タスク共通の既定`allowed_tools`: score-mcpの全8ツール(#43/#44)。タスクごとの
# 絞り込み(例: ghost-sweepにのみBashも許可する)は#50のスコープとし、#49では
# 「run を起動するとツール呼び出しがリアルタイムに SSE で流れる」を満たす
# 最小限の既定値にとどめる。
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = tuple(f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES)


@dataclass(frozen=True)
class TaskDefinition:
    """1タスク定義。`tools`は設計書§8.7の表の「主に使うツール」列(表示用)であり、

    実際にプロバイダへ渡す`allowed_tools`(mcp__score__*形式)とは別物。
    """

    id: str
    purpose: str
    tools: tuple[str, ...]
    turns_min: int | None
    turns_max: int
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS


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
