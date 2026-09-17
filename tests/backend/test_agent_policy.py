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

    @pytest.mark.parametrize(
        "command",
        [
            "python script.py",
            'python -c "import os"',
            "python -i",
            "python -m http.server",
            "python",
            'python -c"import os" /abs/path',
            "python -mhttp.server",
            "python3 script.py",
        ],
    )
    def test_denies_python_entirely(self, tmp_path: Path, command: str) -> None:
        """#45 Gate2レビュー指摘・2巡目 HIGH: 引数検証(`-c`/`-i`/`-m`拒否+

        workspace配下スクリプトのみ許可)を行っても、`-c"..."`のような
        フラグ連結表記でshlexの1トークン化により完全一致チェックを回避
        できてしまうこと、また許可した.py自体がopen('/etc/passwd')等の
        任意I/Oを行えることの2点から、名前ベースの引数検証では構造的な
        安全性を担保できないと判断し、pythonをホワイトリストから完全に
        除外した。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command(command, workspace) is False

    def test_python_is_not_in_whitelist(self) -> None:
        assert "python" not in policy.SHELL_COMMAND_WHITELIST
        assert "python3" not in policy.SHELL_COMMAND_WHITELIST

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

    def test_denies_quoted_absolute_path_outside_workspace(
        self, tmp_path: Path
    ) -> None:
        """#45 Gate2レビュー指摘・3巡目 HIGH: `shlex.split(..., posix=False)`は

        クォートをトークンに残すため、クォートを剥がさずに`within_workspace`
        へ渡すと`"/etc/passwd"`が相対パス扱いになりcwd(=workspace)配下と
        誤判定されていた。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command('cat "/etc/passwd"', workspace) is False
        assert policy.is_allowed_command("cat '/etc/passwd'", workspace) is False

    def test_denies_windows_style_path_outside_workspace(self, tmp_path: Path) -> None:
        """#45 Gate2レビュー指摘・3巡目 HIGH: `_looks_like_path`が`/`しか

        見ていなかったため、バックスラッシュ区切り・ドライブレター付きの
        Windowsパス(本プロジェクトはWindows専用、NFR-08′)が「裸のトークン」
        とみなされworkspace境界チェックを素通りしていた。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command(r"cat C:\Windows\win.ini", workspace) is False
        assert policy.is_allowed_command(r"ls C:\Users", workspace) is False

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.ssh/id_rsa",
            "cat $HOME/.ssh/id_rsa",
            "cat %USERPROFILE%\\.ssh\\id_rsa",
            "cat *.txt",
        ],
    )
    def test_denies_shell_expansion_in_arguments(
        self, tmp_path: Path, command: str
    ) -> None:
        """#45 Gate2レビュー指摘・3巡目 HIGH: `~`/`$`/`%`/グロブ文字は

        リテラル文字列としてはworkspace相対に見えても、シェル展開後は
        workspace外を指しうるため、`_SHELL_METACHARACTERS`に追加した。
        """
        workspace = tmp_path / "agent_workspace" / "run1"
        assert policy.is_allowed_command(command, workspace) is False


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
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
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
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
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
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert entries[0]["decision"] == "deny"
        assert entries[0]["tool_input"]["file_path"] == score_path


class TestRecordToolStart:
    async def test_returns_empty_dict_and_records_start_time(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)

        result = await policy.record_tool_start(
            {"tool_name": "score_context"}, "tu1", {"ctx": ctx}
        )

        assert result == {}
        assert "tu1" in ctx.pending_tool_starts

    async def test_ignores_missing_tool_use_id(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)

        result = await policy.record_tool_start(
            {"tool_name": "score_context"}, None, {"ctx": ctx}
        )

        assert result == {}
        assert ctx.pending_tool_starts == {}


class TestAuditPostToolUse:
    async def test_computes_elapsed_ms_from_recorded_start(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        await policy.record_tool_start(
            {"tool_name": "score_context"}, "tu1", {"ctx": ctx}
        )

        await policy.audit_post_tool_use(
            {"tool_name": "score_context", "tool_input": {}, "tool_response": {}},
            "tu1",
            {"ctx": ctx},
        )

        entries = [
            json.loads(line)
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert entries[0]["elapsed_ms"] is not None
        assert entries[0]["elapsed_ms"] >= 0
        assert "tu1" not in ctx.pending_tool_starts

    async def test_elapsed_ms_is_none_when_start_was_not_recorded(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)

        await policy.audit_post_tool_use(
            {"tool_name": "score_context", "tool_input": {}, "tool_response": {}},
            "tu_unrecorded",
            {"ctx": ctx},
        )

        entries = [
            json.loads(line)
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert entries[0]["elapsed_ms"] is None

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
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
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
        assert pre_matchers == {None, "Bash", "Write", "Edit"}
        assert hooks["PostToolUse"][0].matcher is None

    async def test_wired_hook_closures_delegate_to_ctx(self, tmp_path: Path) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        hooks = policy.build_claude_hooks(ctx)
        bash_matcher = next(m for m in hooks["PreToolUse"] if m.matcher == "Bash")
        bash_hook = bash_matcher.hooks[0]

        result = await bash_hook(
            {"tool_input": {"command": "curl http://evil.example"}}, "tu1", {}
        )

        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert audit_log_path(workspace).exists()

    async def test_elapsed_ms_recorded_via_pre_and_post_hooks(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run1"
        ctx = _ctx(workspace)
        hooks = policy.build_claude_hooks(ctx)
        start_matcher = next(m for m in hooks["PreToolUse"] if m.matcher is None)
        post_matcher = hooks["PostToolUse"][0]

        await start_matcher.hooks[0]({"tool_name": "score_context"}, "tu1", {})
        await post_matcher.hooks[0](
            {"tool_name": "score_context", "tool_input": {}, "tool_response": {}},
            "tu1",
            {},
        )

        entries = [
            json.loads(line)
            for line in audit_log_path(workspace)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert entries[0]["elapsed_ms"] is not None
        assert entries[0]["elapsed_ms"] >= 0


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
