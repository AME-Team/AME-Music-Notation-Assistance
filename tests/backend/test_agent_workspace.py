"""#47: エージェントワークスペースと report.md の単体・結合テスト(§8.6, §10.3, §11.3)。

完了条件:
- タスクごとに使い捨てディレクトリ agent_workspace/{run_id}/ を生成
  - TASK.md (タスク指示)
  - context.md (曲情報サマリ)
  - notation_rules.md (記譜ルール集、L1プロンプトと同一)
  - scratch/ (エージェント自由領域)
  - report.md (必須成果物)
- GET /api/agent/runs/{run_id}/report
- run 終了後のワークスペース保持/削除ポリシーの動作確認
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent.audit import AuditEntry, append_audit_entry
from app.agent.workspace import (
    ReportNotFoundError,
    clean_workspace,
    generate_context_markdown,
    read_report,
    setup_agent_workspace,
    write_report,
)
from app.config import Settings
from app.domain.score import (
    ChordEntry,
    Clef,
    KeySignatureEntry,
    Note,
    Part,
    ScoreIR,
    SourceInfo,
    TempoMapEntry,
    TimeSignatureEntry,
)
from app.infra import storage
from app.pipeline.refine.l1_prompt import NOTATION_RULES_MARKDOWN
from app.services.agent_run_service import AgentRunService
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient


def _dummy_score(project_id: str = "prj_test") -> ScoreIR:
    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="sample.wav", duration_sec=10.0, sample_rate=44100),
        divisions=480,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        tempo_map=[TempoMapEntry(bar=1, beat=1.0, bpm=128.0)],
        key_signatures=[KeySignatureEntry(bar=1, fifths=1, mode="major")],
        chords=[ChordEntry(bar=1, beat=1.0, symbol="Gmaj7", confidence=0.95)],
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )
    n1 = Note(
        id=score.allocate_note_id(),
        onset_sec=0.0,
        duration_sec=0.5,
        onset_tick=0,
        duration_tick=480,
        midi=60,
        velocity=80,
        provenance="baseline",
    )
    n2 = Note(
        id=score.allocate_note_id(),
        onset_sec=0.5,
        duration_sec=0.5,
        onset_tick=480,
        duration_tick=480,
        midi=67,
        velocity=85,
        provenance="baseline",
    )
    part.notes = [n1, n2]
    score.parts = [part]
    return score


class TestAgentWorkspaceGeneration:
    def test_setup_agent_workspace_creates_all_expected_files(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "agent_workspace" / "run_01"
        score = _dummy_score("prj_01")

        setup_agent_workspace(
            workspace,
            task_type="refine-part",
            project_id="prj_01",
            run_id="run_01",
            prompt="ピアノパートの異名同音と声部を整理してください。",
            score=score,
            scope={"part_id": "piano", "bars": [1, 4]},
            allowed_tools=["score_query", "score_apply_ops"],
            model="claude-3-5-sonnet",
            max_turns=30,
        )

        assert workspace.is_dir()
        task_file = workspace / "TASK.md"
        context_file = workspace / "context.md"
        rules_file = workspace / "notation_rules.md"
        scratch_dir = workspace / "scratch"

        assert task_file.exists()
        assert context_file.exists()
        assert rules_file.exists()
        assert scratch_dir.is_dir()

        # TASK.md の内容検証
        task_text = task_file.read_text(encoding="utf-8")
        assert "refine-part" in task_text
        assert "run_01" in task_text
        assert "prj_01" in task_text
        assert "claude-3-5-sonnet" in task_text
        assert "ピアノパートの異名同音と声部を整理してください。" in task_text
        assert "report.md" in task_text
        assert "score_query" in task_text

        # notation_rules.md の内容検証 (SSOT: NOTATION_RULES_MARKDOWN と完全一致)
        rules_text = rules_file.read_text(encoding="utf-8")
        assert rules_text == NOTATION_RULES_MARKDOWN

        # context.md の内容検証
        context_text = context_file.read_text(encoding="utf-8")
        assert "Piano" in context_text
        assert "128.0 BPM" in context_text
        assert "4/4" in context_text
        assert "fifths=1 (長調 (Major))" in context_text
        assert "Gmaj7" in context_text

    def test_context_markdown_estimates_key_when_key_signatures_empty(
        self, tmp_path: Path
    ) -> None:
        score = _dummy_score("prj_02")
        score.key_signatures = []  # 調号なし
        context_md = generate_context_markdown(score)
        assert "推定調:" in context_md

    def test_windows_strict_encoding_and_newlines(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace_crlf_test"
        score = _dummy_score("prj_03")
        setup_agent_workspace(
            workspace,
            task_type="investigate",
            project_id="prj_03",
            run_id="run_03",
            prompt="test",
            score=score,
        )

        for filename in ("TASK.md", "context.md", "notation_rules.md"):
            raw_bytes = (workspace / filename).read_bytes()
            assert b"\r\n" not in raw_bytes, f"{filename} contains CRLF instead of LF"
            assert b"\n" in raw_bytes


class TestReportOperations:
    def test_write_and_read_report(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace_report"
        workspace.mkdir(parents=True)

        # 未作成時は ReportNotFoundError
        with pytest.raises(ReportNotFoundError):
            read_report(workspace)

        content = "# 成果報告\n\n- 異名同音2件修正\n- 声部分離を実施\n"
        path = write_report(workspace, content)
        assert path.exists()

        loaded = read_report(workspace)
        assert loaded == content

        # LF 改行確認
        assert b"\r\n" not in path.read_bytes()


class TestWorkspaceRetentionPolicy:
    def test_clean_workspace_keeps_artifacts_by_default(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace_cleanup"
        score = _dummy_score("prj_clean")
        setup_agent_workspace(
            workspace,
            task_type="refine-part",
            project_id="prj_clean",
            run_id="run_clean",
            prompt="clean test",
            score=score,
        )
        write_report(workspace, "# Report\nCompleted.")
        append_audit_entry(
            workspace,
            AuditEntry(
                run_id="run_clean",
                project_id="prj_clean",
                tool_name="score_query",
                tool_input={},
                decision="allow",
            ),
        )

        # scratch/ に一時ファイル・ディレクトリを作成
        scratch_dir = workspace / "scratch"
        (scratch_dir / "temp_script.py").write_text("print('hello')", encoding="utf-8")
        sub_dir = scratch_dir / "temp_output"
        sub_dir.mkdir()
        (sub_dir / "intermediate.json").write_text("{}", encoding="utf-8")

        # クリーンアップ実行 (keep_artifacts=True)
        clean_workspace(workspace, keep_artifacts=True)

        # scratch 内のファイルは消滅
        assert not (scratch_dir / "temp_script.py").exists()
        assert not sub_dir.exists()

        # report.md, TASK.md, context.md, notation_rules.md, audit.jsonl は保持される
        assert (workspace / "report.md").exists()
        assert (workspace / "TASK.md").exists()
        assert (workspace / "context.md").exists()
        assert (workspace / "notation_rules.md").exists()
        assert (workspace / "audit.jsonl").exists()

    def test_clean_workspace_removes_entirely_when_not_keeping_artifacts(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace_purge"
        score = _dummy_score("prj_purge")
        setup_agent_workspace(
            workspace,
            task_type="refine-part",
            project_id="prj_purge",
            run_id="run_purge",
            prompt="purge test",
            score=score,
        )
        assert workspace.exists()

        clean_workspace(workspace, keep_artifacts=False)
        assert not workspace.exists()

    def test_setup_agent_workspace_clears_stale_files_on_recreation(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace_recreate"
        score = _dummy_score("prj_recreate")

        # 1回目の構築
        setup_agent_workspace(
            workspace,
            task_type="refine-part",
            project_id="prj_recreate",
            run_id="run_recreate",
            prompt="first run",
            score=score,
        )
        # report.md や scratch/ に古いファイルを作成
        write_report(workspace, "old report")
        (workspace / "scratch" / "old_file.txt").write_text("old", encoding="utf-8")

        # 同一ワークスペースで再構築
        setup_agent_workspace(
            workspace,
            task_type="refine-part",
            project_id="prj_recreate",
            run_id="run_recreate",
            prompt="second run",
            score=score,
        )

        # 古い report.md や scratch/old_file.txt は消去されていること
        assert not (workspace / "report.md").exists()
        assert not (workspace / "scratch" / "old_file.txt").exists()
        # 新しい TASK.md は存在
        assert "second run" in (workspace / "TASK.md").read_text(encoding="utf-8")


def _create_project_record(workspace_dir: Path, project_id: str) -> None:
    storage.ensure_project_layout(workspace_dir, project_id)
    from app.infra import db

    conn = db.get_connection(workspace_dir / "db.sqlite3")
    conn.execute(
        "INSERT INTO projects(id, name, original_filename, audio_format, created_at) "
        "VALUES (?, 'Test', 'song.wav', 'wav', '2026-01-01T00:00:00Z')",
        (project_id,),
    )
    conn.commit()


class TestAgentRunServiceWorkspaceIntegration:
    def test_service_create_workspace_and_report_flow(self, settings: Settings) -> None:
        project_id = "proj_srv_01"
        run_id = "run_srv_01"

        _create_project_record(settings.workspace_dir, project_id)
        score_service = ScoreService(settings.workspace_dir)
        score_service.write_score(project_id, _dummy_score(project_id))

        agent_service = AgentRunService(settings.workspace_dir)
        agent_service.create_run(run_id=run_id, project_id=project_id)

        # ワークスペース構築
        workspace = agent_service.create_workspace(
            run_id,
            task_type="refine-part",
            prompt="テストプロンプト",
        )
        assert workspace.is_dir()
        assert (workspace / "TASK.md").exists()

        # レポート書き込みと取得
        with pytest.raises(ReportNotFoundError):
            agent_service.get_report(run_id)

        agent_service.write_report(run_id, "# 完了報告\n全タスク完了")
        report = agent_service.get_report(run_id)
        assert report == "# 完了報告\n全タスク完了"

        # キャンセル実行時に scratch/ がクリーンアップされることを確認
        scratch_file = workspace / "scratch" / "test.txt"
        scratch_file.write_text("temp", encoding="utf-8")
        assert scratch_file.exists()

        agent_service.cancel(run_id)
        assert not scratch_file.exists()
        # report.md はキャンセル後も参照可能
        assert agent_service.get_report(run_id) == "# 完了報告\n全タスク完了"

    @pytest.mark.parametrize("terminal_status", ["completed", "failed", "truncated"])
    def test_terminal_statuses_clean_scratch_workspace(
        self, settings: Settings, terminal_status: str
    ) -> None:
        """run 終了ステータス(completed, failed, truncated)確定時に scratch/ が掃除されることを確認。"""
        project_id = f"proj_term_{terminal_status}"
        run_id = f"run_term_{terminal_status}"

        _create_project_record(settings.workspace_dir, project_id)
        score_service = ScoreService(settings.workspace_dir)
        score_service.write_score(project_id, _dummy_score(project_id))

        agent_service = AgentRunService(settings.workspace_dir)
        agent_service.create_run(run_id=run_id, project_id=project_id)

        workspace = agent_service.create_workspace(
            run_id, task_type="refine-part", prompt="cleanup on end test"
        )
        agent_service.write_report(run_id, "# Done")

        scratch_file = workspace / "scratch" / "data.tmp"
        scratch_file.write_text("intermediate data", encoding="utf-8")
        assert scratch_file.exists()

        # 終了ステータスへ更新
        agent_service.update_status(run_id, terminal_status)  # type: ignore[arg-type]

        # scratch/ の中身は消去され、report.md は保持される
        assert not scratch_file.exists()
        assert (workspace / "report.md").exists()
        assert agent_service.get_report(run_id) == "# Done"


class TestAgentReportApi:
    def test_get_report_api_success(
        self, client: TestClient, settings: Settings
    ) -> None:
        project_id = "proj_api_01"
        run_id = "run_api_01"

        _create_project_record(settings.workspace_dir, project_id)
        score_service = ScoreService(settings.workspace_dir)
        score_service.write_score(project_id, _dummy_score(project_id))

        agent_service = AgentRunService(settings.workspace_dir)
        agent_service.create_run(run_id=run_id, project_id=project_id)
        agent_service.create_workspace(
            run_id, task_type="refine-part", prompt="APIテスト"
        )
        agent_service.write_report(run_id, "## 変更内容\n- 異名同音を変更")

        resp = client.get(f"/api/agent/runs/{run_id}/report")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["run_id"] == run_id
        assert data["content"] == "## 変更内容\n- 異名同音を変更"

    def test_get_report_api_404_when_report_not_found(
        self, client: TestClient, settings: Settings
    ) -> None:
        project_id = "proj_api_02"
        run_id = "run_api_02"

        _create_project_record(settings.workspace_dir, project_id)
        score_service = ScoreService(settings.workspace_dir)
        score_service.write_score(project_id, _dummy_score(project_id))

        agent_service = AgentRunService(settings.workspace_dir)
        agent_service.create_run(run_id=run_id, project_id=project_id)
        agent_service.create_workspace(
            run_id, task_type="refine-part", prompt="未生成テスト"
        )

        resp = client.get(f"/api/agent/runs/{run_id}/report")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_get_report_api_404_when_run_id_unknown(self, client: TestClient) -> None:
        resp = client.get("/api/agent/runs/non_existent_run/report")
        assert resp.status_code == 404
