"""L2エージェントの監査ログ書き込み(#45, NFR-15/FR-22)。

`policy.py`のフックから呼ばれる。書き込み先は`workspace/audit.jsonl`
(`AgentTask.workspace`そのもの、#47が作る`agent_workspace/{run_id}/`直下) —
新しい`storage.py`ヘルパーは不要(既存の`score/ops.jsonl`はノート編集の
Undo/Redo監査用であり、こちらは「全ツール呼び出し」という別の監査対象の
ため、`score_ops_log_path`とは意図的に分離する)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuditEntry(BaseModel):
    """ツール呼び出し1件分の監査記録(引数・決定・結果・所要時間)。"""

    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    run_id: str
    project_id: str
    tool_name: str
    tool_input: dict[str, Any]
    decision: Literal["allow", "deny"]
    reason: str | None = None
    result: Any | None = None
    elapsed_ms: float | None = None


def audit_log_path(workspace: Path) -> Path:
    return workspace / "audit.jsonl"


def append_audit_entry(workspace: Path, entry: AuditEntry) -> None:
    """`workspace`が無ければ作成してから追記する(#47の完了を待たない)。

    `storage.append_jsonl`と同じ書式(UTF-8、LF、1行1JSON)にするため
    直接同じ処理を行う — `storage.py`は`workspace_dir`/`project_id`起点の
    パス規約を前提にしており、エージェントワークスペース(`AgentTask.workspace`
    という別系統のルート)には合わないため、ここで完結させる。
    """
    path = audit_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(entry.model_dump_json())
        f.write("\n")
