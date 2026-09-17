"""L2 Coding Agent のワークスペース管理と成果物レポート(#47, 設計書§8.6/§10.3/§11.3)。

エージェント実行ごとに使い捨てのワークスペースディレクトリを生成する:
  agent_workspace/{run_id}/  (実体: workspace/{project_id}/agent/{run_id}/)
  ├─ TASK.md              # タスク指示(人が読める形。エージェントが最初に読む)
  ├─ context.md           # 曲情報サマリ(調・拍子・テンポ・構成・パート編成)
  ├─ notation_rules.md    # 記譜ルール集(L1のシステムプロンプトと同じ内容)
  ├─ scratch/             # エージェントの自由領域(分析スクリプト・中間結果)
  ├─ audit.jsonl          # ツール呼び出し監査ログ(#45)
  └─ report.md            # ★エージェントが最後に書く成果報告(UIのDiffPanelに表示)

保持/削除ポリシー:
- 実行終了時(completed/failed/truncated)およびキャンセル時(cancelled):
  - `report.md` と `audit.jsonl` は人間によるレビューおよび監査のために保持する。
  - `scratch/` 内の一時ファイルはクリーンアップ可能(`clean_workspace(keep_artifacts=True)`)。
  - プロジェクト削除時にはプロジェクト配下ごとカスケード削除される。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.domain.score import ScoreIR
from app.pipeline.refine.key_estimation import estimate_key
from app.pipeline.refine.l1_prompt import NOTATION_RULES_MARKDOWN


class ReportNotFoundError(FileNotFoundError):
    """report.md がまだ生成されていないか存在しない場合。"""


def get_report_path(workspace: Path) -> Path:
    """エージェントワークスペース直下の report.md のパスを返す。"""
    return workspace / "report.md"


def read_report(workspace: Path) -> str:
    """エージェントワークスペース直下の report.md を読み込む。"""
    path = get_report_path(workspace)
    if not path.exists() or not path.is_file():
        raise ReportNotFoundError(f"report.md not found in {workspace}")
    return path.read_text(encoding="utf-8")


def write_report(workspace: Path, content: str) -> Path:
    """エージェントワークスペース直下に report.md を書き込む(Windows専用化: UTF-8, LF)。"""
    path = get_report_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def generate_task_markdown(
    *,
    task_type: str,
    project_id: str,
    run_id: str,
    prompt: str,
    scope: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
    max_turns: int | None = None,
) -> str:
    """人が読める形のエージェント指示書(TASK.md)を生成する。"""
    lines = [
        f"# エージェントタスク指示 (TASK.md) — {task_type}",
        "",
        "## 実行メタデータ",
        f"- Run ID: `{run_id}`",
        f"- プロジェクトID: `{project_id}`",
        f"- タスク種別: `{task_type}`",
    ]
    if model:
        lines.append(f"- 使用モデル: `{model}`")
    if max_turns:
        lines.append(f"- 最大ターン数: `{max_turns}`")
    if scope:
        lines.append(f"- スコープ: `{scope}`")
    if allowed_tools:
        lines.append(f"- 許可ツール: {', '.join(f'`{t}`' for t in allowed_tools)}")
    lines.append("")

    lines.append("## 指示内容 (Prompt)")
    lines.append(prompt.strip() if prompt else "(指示なし)")
    lines.append("")

    lines.append("## 成果物要件 (重要)")
    lines.append("本タスクの必須成果物は `report.md` です。")
    lines.append("終了前に必ずワークスペース直下に `report.md` を作成してください。")
    lines.append("`report.md` には以下の内容を含めてください:")
    lines.append("1. **実施概要**: 何を目的として何を行ったか")
    lines.append("2. **変更箇所と判断理由**: 各変更(異名同音・声部・タイ等)の理由を人間可読で説明")
    lines.append("3. **注意点・懸念点**: 演奏者やユーザーがレビュー時に注意すべき点")
    lines.append("")
    lines.append("※ この `report.md` は UI の Diff Panel で判断理由として提示されます。")
    lines.append("")

    lines.append("## ワークスペース構成")
    lines.append("- `TASK.md`: 本指示書")
    lines.append("- `context.md`: 楽曲情報サマリ(調・拍子・テンポ・パート編成)")
    lines.append("- `notation_rules.md`: 記譜ルール集(異名同音・声部・タイ等のルール)")
    lines.append("- `scratch/`: 自由作業領域(スクリプト実行や中間出力など)")
    lines.append("- `report.md`: ★必須成果物(完了時に必ず作成)")
    lines.append("")

    return "\n".join(lines) + "\n"


def generate_context_markdown(score: ScoreIR) -> str:
    """ScoreIR から曲情報サマリ(context.md)を生成する(調・拍子・テンポ・構成・パート編成)。"""
    lines = ["# 楽曲コンテキスト (context.md)", ""]

    # 1. 基本情報
    lines.append("## 基本情報")
    lines.append(f"- プロジェクトID: {score.project_id}")
    lines.append(f"- 音源ファイル名: {score.source.filename}")
    lines.append(f"- 音源長: {score.source.duration_sec:.2f} 秒")
    lines.append(f"- サンプルレート: {score.source.sample_rate} Hz")
    lines.append(f"- Divisions (四分音符あたりのtick数): {score.divisions}")
    lines.append("")

    # 2. 調 (Key)
    lines.append("## 調 (Key)")
    if score.key_signatures:
        for ks in score.key_signatures:
            mode_str = "長調 (Major)" if ks.mode == "major" else "短調 (Minor)"
            lines.append(f"- 第{ks.bar}小節〜: fifths={ks.fifths} ({mode_str})")
    else:
        # パートノートから調推定
        all_pitch_classes = [
            n.midi % 12 for p in score.parts for n in p.notes if n.status != "deleted"
        ]
        if all_pitch_classes:
            key_est = estimate_key(all_pitch_classes)
            lines.append(f"- 推定調: {key_est.label} (信頼度 {key_est.confidence:.2f})")
        else:
            lines.append("- 調情報なし (未設定)")
    lines.append("")

    # 3. 拍子 (Time Signature)
    lines.append("## 拍子 (Time Signature)")
    if score.time_signatures:
        for ts in score.time_signatures:
            lines.append(f"- 第{ts.bar}小節〜: {ts.numerator}/{ts.denominator}")
    else:
        lines.append("- 4/4 (既定)")
    lines.append("")

    # 4. テンポ (Tempo)
    lines.append("## テンポ (Tempo)")
    if score.tempo_map:
        for tm in score.tempo_map:
            lines.append(f"- 第{tm.bar}小節 (beat {tm.beat}): {tm.bpm:.1f} BPM")
    else:
        lines.append("- 120.0 BPM (既定)")
    lines.append("")

    # 5. パート編成 (Part Instrumentation)
    lines.append("## パート編成")
    if score.parts:
        lines.append("| パートID | パート名 | 譜表数 | MIDI Program | ノート数 | 音域 |")
        lines.append("|---|---|---|---|---|---|")
        for part in score.parts:
            active_notes = [n for n in part.notes if n.status != "deleted"]
            note_count = len(active_notes)
            if active_notes:
                min_midi = min(n.midi for n in active_notes)
                max_midi = max(n.midi for n in active_notes)
                pitch_range = f"MIDI {min_midi}〜{max_midi}"
            else:
                pitch_range = "ノートなし"
            p_info = (
                f"| {part.id} | {part.name} | {part.staves} | "
                f"{part.midi_program} | {note_count} | {pitch_range} |"
            )
            lines.append(p_info)
    else:
        lines.append("- パートなし")
    lines.append("")

    # 6. 和声・コード進行サマリ (Chords)
    if score.chords:
        lines.append("## コード進行サマリ")
        chord_summaries = [f"第{c.bar}小節(拍{c.beat}): {c.symbol}" for c in score.chords[:30]]
        for cs in chord_summaries:
            lines.append(f"- {cs}")
        if len(score.chords) > 30:
            lines.append(f"- ... 他 {len(score.chords) - 30} 件")
        lines.append("")

    return "\n".join(lines) + "\n"


def setup_agent_workspace(
    workspace: Path,
    *,
    task_type: str,
    project_id: str,
    run_id: str,
    prompt: str,
    score: ScoreIR,
    scope: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
    max_turns: int | None = None,
) -> Path:
    """§8.6: 使い捨てワークスペースディレクトリを生成し、初期ファイルを配置する。

    生成されるファイル/ディレクトリ:
      - TASK.md (タスク指示)
      - context.md (楽曲コンテキスト)
      - notation_rules.md (記譜ルール集、l1_prompt.NOTATION_RULES_MARKDOWN と同一)
      - scratch/ (自由領域)
    """
    # 再実行時の残留ファイル(前回のreport.mdやscratch/等)の混入を防ぐため、
    # 既存ディレクトリが存在する場合は完全にクリアして再生成する
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)

    # 1. TASK.md
    task_md = generate_task_markdown(
        task_type=task_type,
        project_id=project_id,
        run_id=run_id,
        prompt=prompt,
        scope=scope,
        allowed_tools=allowed_tools,
        model=model,
        max_turns=max_turns,
    )
    (workspace / "TASK.md").write_text(task_md, encoding="utf-8", newline="\n")

    # 2. context.md
    context_md = generate_context_markdown(score)
    (workspace / "context.md").write_text(context_md, encoding="utf-8", newline="\n")

    # 3. notation_rules.md (L1プロンプトと同一のSSOT)
    (workspace / "notation_rules.md").write_text(
        NOTATION_RULES_MARKDOWN, encoding="utf-8", newline="\n"
    )

    # 4. scratch/
    (workspace / "scratch").mkdir(parents=True, exist_ok=True)

    return workspace


def clean_workspace(workspace: Path, *, keep_artifacts: bool = True) -> None:
    """エージェントワークスペースの保持/削除ポリシーを適用する。

    Args:
        workspace: エージェントワークスペースのパス
        keep_artifacts: True の場合、report.md, audit.jsonl, TASK.md などの成果物を保持し、
                       scratch/ の一時作業ファイルのみを削除してディスクを節約する。
                       False の場合、ワークスペース全体を物理削除する。
    """
    if not workspace.exists():
        return

    if not keep_artifacts:
        shutil.rmtree(workspace, ignore_errors=True)
        return

    scratch_dir = workspace / "scratch"
    if scratch_dir.exists() and scratch_dir.is_dir():
        for item in scratch_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
