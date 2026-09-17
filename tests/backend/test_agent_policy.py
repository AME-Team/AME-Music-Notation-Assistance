"""#45: L2エージェントのサンドボックス・権限ポリシー(`agent/policy.py`)のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agent import policy
from app.agent.audit import audit_log_path


def _ctx(workspace: Path) -> policy.PolicyContext:
    return policy.PolicyContext(workspace=workspace, run_id="run1", project_id="prj1")


class TestIsAllowedCommand:
    @pytest.mark.parametrize(
        "command",
        ["ls", "ls -la", "cat foo.txt", "grep bar baz.txt"],
    )
    def test_allows_whitelisted_commands_with_bare_args(
        self, tmp_path: Path, command: str
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command(command, workspace) is True

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "curl http://evil.example",
            "git push",
            "pip install foo",
            "ls; rm -rf /",
            "ls && curl http://evil.example",
            "ls | curl http://evil.example",
            "ls `curl http://evil.example`",
            "ls $(curl http://evil.example)",
            "ls\ncurl http://evil.example",
            "ls\rcurl http://evil.example",
            "",
            "   ",
        ],
    )
    def test_rejects_non_whitelisted_or_chained_commands(
        self, tmp_path: Path, command: str
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command(command, workspace) is False

    def test_rejects_whitelisted_binary_invoked_via_absolute_path_trick(
        self, tmp_path: Path
    ) -> None:
        """先頭コマンド名(basename)だけを見るため、実行ファイル名自体が

        ホワイトリスト外であれば拒否される(`/usr/bin/curl`はcurlなので拒否)。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert (
            policy.is_allowed_command("/usr/bin/curl http://evil.example", workspace)
            is False
        )

    def test_rejects_malformed_quoting(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command("ls 'unterminated", workspace) is False

    def test_allows_python_script_inside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        workspace.mkdir(parents=True)
        script = workspace / "scratch" / "run.py"

        assert policy.is_allowed_command(f"python {script}", workspace) is True

    def test_denies_python_inline_code_execution(self, tmp_path: Path) -> None:
        """#45 Gate2レビュー指摘・1巡目 HIGH: `-c`は実行ファイル名の

        ホワイトリスト判定だけでは検出できず、事実上任意コード実行の抜け道
        になっていた。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command('python -c "import os"', workspace) is False

    def test_denies_python_interactive_mode(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command("python -i", workspace) is False

    def test_denies_python_module_flag(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command("python -m http.server", workspace) is False

    def test_denies_bare_python_repl(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command("python", workspace) is False

    def test_denies_python_script_outside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        outside_script = tmp_path / "elsewhere" / "run.py"

        assert policy.is_allowed_command(f"python {outside_script}", workspace) is False

    def test_denies_cat_reading_outside_workspace(self, tmp_path: Path) -> None:
        """#45 Gate2レビュー指摘・1巡目 HIGH: 引数を検証しないと

        `cat`/`grep`/`ls`がworkspace外の任意ファイル読み取りに使えてしまう。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command("cat /etc/passwd", workspace) is False

    def test_denies_ls_traversal_outside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command("ls ../../etc", workspace) is False

    def test_allows_cat_reading_inside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        workspace.mkdir(parents=True)
        target = workspace / "scratch" / "notes.txt"

        assert policy.is_allowed_command(f"cat {target}", workspace) is True


class TestWithinWorkspace:
    def test_allows_path_inside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        workspace.mkdir(parents=True)
        target = workspace / "scratch" / "notes.txt"

        assert policy.within_workspace(str(target), workspace) is True

    def test_rejects_path_outside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        workspace.mkdir(parents=True)
        outside = tmp_path / "workspace" / "prj1" / "score" / "current.json"

        assert policy.within_workspace(str(outside), workspace) is False

    def test_rejects_path_traversal_escaping_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        workspace.mkdir(parents=True)
        traversal = str(
            workspace / ".." / ".." / "workspace" / "prj1" / "score" / "current.json"
        )

        assert policy.within_workspace(traversal, workspace) is False


class TestGuardBash:
    async def test_allows_whitelisted_command_and_records_audit_entry(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)

        result = await policy.guard_bash(
            {"tool_input": {"command": "ls -la"}}, "tu1", {"ctx": ctx}
        )

        assert result == {}
        entries = [
            json.loads(line)
            for line in audit_log_path(workspace).read_text().splitlines()
        ]
        assert entries[0]["decision"] == "allow"
        assert entries[0]["tool_name"] == "Bash"

    async def test_denies_non_whitelisted_command_with_reason(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)

        result = await policy.guard_bash(
            {"tool_input": {"command": "curl http://evil.example"}}, "tu1", {"ctx": ctx}
        )

        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "curl" in result["hookSpecificOutput"]["permissionDecisionReason"]
        entries = [
            json.loads(line)
            for line in audit_log_path(workspace).read_text().splitlines()
        ]
        assert entries[0]["decision"] == "deny"


class TestGuardWrite:
    async def test_allows_write_inside_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        workspace.mkdir(parents=True)
        ctx = _ctx(workspace)
        target = str(workspace / "scratch" / "notes.txt")

        result = await policy.guard_write(
            {"tool_name": "Write", "tool_input": {"file_path": target}},
            "tu1",
            {"ctx": ctx},
        )

        assert result == {}

    async def test_denies_direct_score_current_json_write_with_guidance(
        self, tmp_path: Path
    ) -> None:
        """#45の完了条件: score/current.jsonへの直接書き込みは拒否され、

        監査ログに残る。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        score_path = str(tmp_path / "workspace" / "prj1" / "score" / "current.json")

        result = await policy.guard_write(
            {"tool_name": "Write", "tool_input": {"file_path": score_path}},
            "tu1",
            {"ctx": ctx},
        )

        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (
            "score_apply_ops"
            in result["hookSpecificOutput"]["permissionDecisionReason"]
        )

        entries = [
            json.loads(line)
            for line in audit_log_path(workspace).read_text().splitlines()
        ]
        assert entries[0]["decision"] == "deny"
        assert entries[0]["tool_input"]["file_path"] == score_path


class TestAuditPostToolUse:
    async def test_records_result_for_every_tool_call(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)

        result = await policy.audit_post_tool_use(
            {
                "tool_name": "score_context",
                "tool_input": {},
                "tool_response": {"key_signatures": []},
            },
            "tu1",
            {"ctx": ctx},
        )

        assert result == {}
        entries = [
            json.loads(line)
            for line in audit_log_path(workspace).read_text().splitlines()
        ]
        assert entries[0]["tool_name"] == "score_context"
        assert entries[0]["result"] == {"key_signatures": []}

    async def test_resets_streak_on_successful_score_apply_ops(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        ctx.consecutive_score_apply_ops_failures = 2

        result = await policy.audit_post_tool_use(
            {
                "tool_name": "mcp__score__score_apply_ops",
                "tool_input": {},
                "tool_response": {"ok": True, "violations": []},
            },
            "tu1",
            {"ctx": ctx},
        )

        assert result == {}
        assert ctx.consecutive_score_apply_ops_failures == 0

    async def test_forces_abort_after_three_consecutive_failures(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        failing_response = {
            "tool_name": "mcp__score__score_apply_ops",
            "tool_input": {},
            "tool_response": {"ok": False, "violations": [{"rule": "V-8"}]},
        }

        r1 = await policy.audit_post_tool_use(failing_response, "tu1", {"ctx": ctx})
        r2 = await policy.audit_post_tool_use(failing_response, "tu2", {"ctx": ctx})
        r3 = await policy.audit_post_tool_use(failing_response, "tu3", {"ctx": ctx})

        assert r1 == {}
        assert r2 == {}
        assert r3["continue"] is False
        assert "stopReason" in r3

    async def test_non_consecutive_failures_do_not_trigger_abort(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        failing = {
            "tool_name": "mcp__score__score_apply_ops",
            "tool_input": {},
            "tool_response": {"ok": False, "violations": []},
        }
        succeeding = {
            "tool_name": "mcp__score__score_apply_ops",
            "tool_input": {},
            "tool_response": {"ok": True, "violations": []},
        }

        await policy.audit_post_tool_use(failing, "tu1", {"ctx": ctx})
        await policy.audit_post_tool_use(failing, "tu2", {"ctx": ctx})
        await policy.audit_post_tool_use(succeeding, "tu3", {"ctx": ctx})
        r4 = await policy.audit_post_tool_use(failing, "tu4", {"ctx": ctx})

        assert r4 == {}


class TestBuildClaudeHooks:
    def test_returns_hooks_for_pretooluse_and_posttooluse(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path / "agent_workspace" / "run1")

        hooks = policy.build_claude_hooks(ctx)

        assert set(hooks.keys()) == {"PreToolUse", "PostToolUse"}
        pre_matchers = {m.matcher for m in hooks["PreToolUse"]}
        assert pre_matchers == {"Bash", "Write", "Edit"}
        assert hooks["PostToolUse"][0].matcher is None

    async def test_wired_hook_closures_delegate_to_ctx(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        hooks = policy.build_claude_hooks(ctx)
        bash_hook = hooks["PreToolUse"][0].hooks[0]

        result = await bash_hook(
            {"tool_input": {"command": "curl http://evil.example"}}, "tu1", {}
        )

        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert audit_log_path(workspace).exists()


class TestBuildOpencodeToolsConfig:
    def test_enables_bash_write_edit_matching_claude_side_intent(self) -> None:
        """#45 Gate2レビュー指摘・1巡目 MIDDLE: 一律Falseで全無効化すると

        OpenCode版エージェントが一切作業できず、Claude側(ホワイトリスト
        準拠のBashとworkspace内Write/Editを許可)と挙動が乖離してしまう。
        """
        config = policy.build_opencode_tools_config()

        assert config["tools"]["bash"] is True
        assert config["tools"]["write"] is True
        assert config["tools"]["edit"] is True

    def test_disables_network_tool(self) -> None:
        config = policy.build_opencode_tools_config()

        assert config["tools"]["webfetch"] is False


class TestExceedsTokenBudget:
    def test_true_when_over_budget(self) -> None:
        assert policy.exceeds_token_budget(1000, 500) is True

    def test_false_when_within_budget(self) -> None:
        assert policy.exceeds_token_budget(100, 500) is False
