"""score-mcp インプロセスアダプタ(#44, 設計書§8.3/§8.4)— Claude Agent SDK用。

`tools.py`(#43)のロジックには一切手を入れず、`@tool`+`create_sdk_mcp_server`
でラップするだけの薄いレイヤ(§8.4「アダプタは数十行の薄いラッパ」)。

例外(`tools.ToolError`)は各ハンドラでcatchせず素通しする: `create_sdk_mcp_server`
も(stdio用の`mcp.server.fastmcp.FastMCP`と同様)最終的に`mcp.server.Server`の
ワイヤープロトコルハンドラを経由し、そこが例外を自動的にMCPの`isError: true`
応答へ変換する(`mcp.server.lowlevel.server.Server.call_tool`の実装で確認済み)。
SDKドキュメントの`divide`の例が示す手動`is_error`処理は必須ではなく任意の
選択肢の一つである。
"""

from __future__ import annotations

import json
from typing import Any, NotRequired, TypedDict

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from app.agent.mcp import tools
from app.agent.mcp._schema import as_bar_range
from app.agent.mcp.tools import ToolContext

SERVER_NAME = "score"
TOOL_NAMES: tuple[str, ...] = (
    "score_query",
    "score_context",
    "score_stats",
    "score_validate",
    "score_render",
    "baseline_diff",
    "score_note_history",
    "score_apply_ops",
)


# ---------------------------------------------------------------------------
# 引数スキーマ(TypedDict)。tools.pyの各関数のキーワード専用引数と1:1対応させる。
# plain dict(name→型)方式は全キーが自動的にrequiredになる(claude_agent_sdk
# 実装で確認済み)ため、任意引数を持つツールにはTypedDict+NotRequiredを使う。
# ---------------------------------------------------------------------------


class ScoreQueryArgs(TypedDict):
    part: str
    bars: list[int]
    filter: NotRequired[dict[str, Any] | None]


class ScoreStatsArgs(TypedDict):
    part: str
    metric: str
    bars: NotRequired[list[int] | None]


class ScoreValidateArgs(TypedDict):
    scope: NotRequired[dict[str, Any] | None]


class ScoreRenderArgs(TypedDict):
    scope: dict[str, Any] | None
    format: str


class BaselineDiffArgs(TypedDict):
    scope: NotRequired[dict[str, Any] | None]


class ScoreNoteHistoryArgs(TypedDict):
    note_id: int


class ScoreApplyOpsArgs(TypedDict):
    ops: list[dict[str, Any]]


def _to_content(result: Any) -> dict[str, Any]:
    """tools.pyの戻り値をMCPの`{"content": [...]}`形式へ変換する。

    `score_render(format="musicxml")`だけは素の`str`(XML文字列)を返すため、
    それ以外(dict/list)は`json.dumps`してから包む。
    """
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def build_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    """`ctx`を束縛した8個の`SdkMcpTool`を返す。

    テストが実MCPプロトコル層を経由せず`tool.handler(args)`を直接呼べるよう、
    `build_sdk_mcp_server`から分離して公開する。
    """

    @tool(
        "score_query",
        "指定part・小節範囲のノート配列(snap候補・flags含む)を返す",
        ScoreQueryArgs,
    )
    async def score_query(args: ScoreQueryArgs) -> dict[str, Any]:
        result = tools.score_query(
            ctx,
            part=args["part"],
            bars=as_bar_range(args["bars"]),
            filter=args.get("filter"),
        )
        return _to_content(result)

    @tool("score_context", "調・拍子・テンポマップ・コード進行を返す", {})
    async def score_context(args: dict[str, Any]) -> dict[str, Any]:
        return _to_content(tools.score_context(ctx))

    @tool(
        "score_stats",
        "onset分布・音域・和音密度・ベロシティ分布の統計を返す",
        ScoreStatsArgs,
    )
    async def score_stats(args: ScoreStatsArgs) -> dict[str, Any]:
        bars = args.get("bars")
        result = tools.score_stats(
            ctx,
            part=args["part"],
            metric=args["metric"],  # type: ignore[arg-type]
            bars=as_bar_range(bars) if bars is not None else None,
        )
        return _to_content(result)

    @tool("score_validate", "スコアの現状を検証し違反一覧(V-1〜V-10)を返す", ScoreValidateArgs)
    async def score_validate(args: ScoreValidateArgs) -> dict[str, Any]:
        result = tools.score_validate(ctx, scope=args.get("scope"))
        return _to_content(result)

    @tool("score_render", "MusicXML文字列、または軽量な記譜統計を返す", ScoreRenderArgs)
    async def score_render(args: ScoreRenderArgs) -> dict[str, Any]:
        result = tools.score_render(
            ctx,
            scope=args["scope"],
            format=args["format"],  # type: ignore[arg-type]
        )
        return _to_content(result)

    @tool("baseline_diff", "L0ベースラインとの差分を返す(#43時点では未実装)", BaselineDiffArgs)
    async def baseline_diff(args: BaselineDiffArgs) -> dict[str, Any]:
        result = tools.baseline_diff(ctx, scope=args.get("scope"))
        return _to_content(result)

    @tool("score_note_history", "指定ノートの変更履歴を返す", ScoreNoteHistoryArgs)
    async def score_note_history(args: ScoreNoteHistoryArgs) -> dict[str, Any]:
        result = tools.score_note_history(ctx, note_id=args["note_id"])
        return _to_content(result)

    @tool(
        "score_apply_ops",
        "検証付きで楽譜編集オペレーションを適用する(唯一の書き込みツール)",
        ScoreApplyOpsArgs,
    )
    async def score_apply_ops(args: ScoreApplyOpsArgs) -> dict[str, Any]:
        result = tools.score_apply_ops(ctx, ops=args["ops"])
        return _to_content(result)

    return [
        score_query,
        score_context,
        score_stats,
        score_validate,
        score_render,
        baseline_diff,
        score_note_history,
        score_apply_ops,
    ]


def build_sdk_mcp_server(ctx: ToolContext) -> McpSdkServerConfig:
    """#48(ClaudeAgentProvider)が`ClaudeAgentOptions.mcp_servers`へ渡す唯一の入口。

    使用例(設計書§8.3): `ClaudeAgentOptions(mcp_servers={"score": build_sdk_mcp_server(ctx)},
    allowed_tools=[f"mcp__score__{name}" for name in TOOL_NAMES])`。
    """
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=build_tools(ctx))
