"""score-mcp stdioアダプタ(#44, 設計書§8.4)— OpenCode用。

`python -m app.agent.mcp.stdio_server`として別プロセスで起動される。
`ToolContext`はインプロセス版(`sdk_adapter.py`)と違い同一プロセス内で直接
構築できないため、環境変数から復元する。

`tools.py`(#43)のロジックには一切手を入れない。`mcp.server.fastmcp.FastMCP`は
`tools.py`の同期関数をそのまま`add_tool`でき、Python型ヒントから自動で
JSON Schemaを生成するため、明示的なスキーマ定義は不要(`sdk_adapter.py`が
TypedDictを要するのはClaude Agent SDK固有の制約であり、こちらには無い)。

例外(`tools.ToolError`)もcatchせず素通しする。`FastMCP`は最終的に
`sdk_adapter.py`と同じ`mcp.server.Server`のワイヤープロトコルハンドラを
経由し、そこが例外を自動的にMCPの`isError: true`応答へ変換する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.agent.mcp import tools
from app.agent.mcp._schema import as_bar_range
from app.agent.mcp.tools import ToolContext

_ENV_WORKSPACE_DIR = "AME_WORKSPACE_DIR"
_ENV_PROJECT_ID = "AME_PROJECT_ID"
_ENV_RUN_ID = "AME_RUN_ID"


def context_from_env() -> ToolContext:
    """別プロセスとして起動されたこのサーバの`ToolContext`を環境変数から復元する。

    `AME_WORKSPACE_DIR`(バックエンドのデータルート、`settings.workspace_dir`に
    相当。エージェント自身のスクラッチ用`AgentTask.workspace`とは別物)は
    設計書§8.4の`opencode.json`例には無いが、`ToolContext`の構築に必須のため
    #44の実装判断として追加する(実際の起動時の設定は#52が担当)。
    """
    return ToolContext(
        workspace_dir=Path(os.environ[_ENV_WORKSPACE_DIR]),
        project_id=os.environ[_ENV_PROJECT_ID],
        run_id=os.environ[_ENV_RUN_ID],
    )


def build_server(ctx: ToolContext) -> FastMCP:
    """`ctx`を束縛した8個のツールを登録済みの`FastMCP`インスタンスを返す。

    各ラッパー関数は`ctx`を含まないシグネチャにする(`FastMCP.add_tool`は
    渡された関数のPython型ヒントから直接JSON Schemaを生成するため、`ctx`が
    残っているとスキーマに誤って混入してしまう)。
    """
    mcp = FastMCP("score-mcp")

    def score_query(
        part: str,
        bars: list[int],
        filter: dict[str, Any] | None = None,  # noqa: A002
    ) -> list[dict[str, Any]]:
        return tools.score_query(ctx, part=part, bars=as_bar_range(bars), filter=filter)

    mcp.add_tool(
        score_query, name="score_query", description="指定part・小節範囲のノート配列を返す"
    )

    def score_context() -> dict[str, Any]:
        return tools.score_context(ctx)

    mcp.add_tool(
        score_context, name="score_context", description="調・拍子・テンポマップ・コード進行を返す"
    )

    def score_stats(part: str, metric: str, bars: list[int] | None = None) -> dict[str, Any]:
        return tools.score_stats(
            ctx,
            part=part,
            metric=metric,  # type: ignore[arg-type]
            bars=as_bar_range(bars) if bars is not None else None,
        )

    mcp.add_tool(
        score_stats,
        name="score_stats",
        description="onset分布・音域・和音密度・ベロシティ分布の統計を返す",
    )

    def score_validate(scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return tools.score_validate(ctx, scope=scope)

    mcp.add_tool(
        score_validate,
        name="score_validate",
        description="スコアの現状を検証し違反一覧(V-1〜V-10)を返す",
    )

    def score_render(scope: dict[str, Any] | None, format: str) -> Any:  # noqa: A002
        return tools.score_render(ctx, scope=scope, format=format)  # type: ignore[arg-type]

    mcp.add_tool(
        score_render, name="score_render", description="MusicXML文字列、または軽量な記譜統計を返す"
    )

    def baseline_diff(scope: dict[str, Any] | None = None) -> dict[str, Any]:
        return tools.baseline_diff(ctx, scope=scope)

    mcp.add_tool(
        baseline_diff,
        name="baseline_diff",
        description="L0ベースラインとの差分を返す(#43時点では未実装)",
    )

    def score_note_history(note_id: int) -> list[dict[str, Any]]:
        return tools.score_note_history(ctx, note_id=note_id)

    mcp.add_tool(
        score_note_history, name="score_note_history", description="指定ノートの変更履歴を返す"
    )

    def score_apply_ops(ops: list[dict[str, Any]]) -> dict[str, Any]:
        return tools.score_apply_ops(ctx, ops=ops)

    mcp.add_tool(
        score_apply_ops,
        name="score_apply_ops",
        description="検証付きで楽譜編集オペレーションを適用する(唯一の書き込みツール)",
    )

    return mcp


def opencode_mcp_config(*, project_id: str, run_id: str, workspace_dir: Path) -> dict[str, Any]:
    """設計書§8.4の`opencode.json`例に対応する`mcp.score`設定断片を返す(#44自身の

    チェックリスト項目「run ごとの opencode.json 生成」)。実際にrun全体の
    `opencode.json`へマージして起動するのは#52の担当。

    `command`は`sys.executable`(このプロセスを起動したPythonインタプリタの
    絶対パス)を使う(#44 Gate2レビュー指摘: 固定文字列`"python"`だと
    `python3`しか無い環境や別の仮想環境が`PATH`優先になる環境で解決に
    失敗しうる)。`-m app.agent.mcp.stdio_server`の実行には`cwd`/`PYTHONPATH`
    が`backend/`を指している必要があるが、プロセスの起動自体(cwd指定含む)は
    #52の担当のためここでは踏み込まない。
    """
    return {
        "score": {
            "type": "local",
            "command": [sys.executable, "-m", "app.agent.mcp.stdio_server"],
            "environment": {
                _ENV_WORKSPACE_DIR: str(workspace_dir),
                _ENV_PROJECT_ID: project_id,
                _ENV_RUN_ID: run_id,
            },
            "enabled": True,
            "timeout": 30000,
        }
    }


def main() -> None:
    build_server(context_from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
