"""#45: L2エージェントの監査ログ書き込み(`agent/audit.py`)のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.audit import AuditEntry, append_audit_entry, audit_log_path


def _entry(**overrides: object) -> AuditEntry:
    defaults: dict[str, object] = {
        "run_id": "run1",
        "project_id": "prj1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "decision": "allow",
    }
    defaults.update(overrides)
    return AuditEntry(**defaults)  # type: ignore[arg-type]


def test_creates_workspace_directory_if_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "agent_workspace" / "run1"
    assert not workspace.exists()

    append_audit_entry(workspace, _entry())

    assert audit_log_path(workspace).exists()


def test_appends_multiple_entries_as_separate_json_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "agent_workspace" / "run1"

    append_audit_entry(workspace, _entry(tool_name="Bash"))
    append_audit_entry(
        workspace, _entry(tool_name="Write", decision="deny", reason="denied")
    )

    lines = audit_log_path(workspace).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["tool_name"] == "Bash"
    assert first["decision"] == "allow"
    assert second["tool_name"] == "Write"
    assert second["decision"] == "deny"
    assert second["reason"] == "denied"


def test_entry_includes_result_and_elapsed_ms() -> None:
    entry = _entry(
        tool_name="mcp__score__score_apply_ops", result={"ok": True}, elapsed_ms=12.5
    )
    assert entry.result == {"ok": True}
    assert entry.elapsed_ms == 12.5


def test_ts_is_auto_populated_when_omitted() -> None:
    entry = _entry()
    assert entry.ts
