"""workspace/{project_id}/ のレイアウト操作(#12 §10.3)。

#79 Windows専用化の方針:
- パス結合は必ず pathlib.Path を使い、文字列連結しない。
- 全ファイル I/O は encoding="utf-8" を明示する。
- 生成する .json / .jsonl は改行コードを LF に統一する(newline="\\n")。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STAGE_SUBDIRS = ("stems", "analysis", "llm", "export")


def project_dir(workspace_dir: Path, project_id: str) -> Path:
    return workspace_dir / project_id


def ensure_project_layout(workspace_dir: Path, project_id: str) -> Path:
    """§10.3 のディレクトリレイアウトを作成する。"""
    base = project_dir(workspace_dir, project_id)
    for sub in STAGE_SUBDIRS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / "score" / "staging").mkdir(parents=True, exist_ok=True)
    (base / "score" / "revisions").mkdir(parents=True, exist_ok=True)
    (base / "agent").mkdir(parents=True, exist_ok=True)
    return base


def original_audio_path(workspace_dir: Path, project_id: str, audio_format: str) -> Path:
    """原曲は `source.{ext}` の固定名で保存する(#79: 日本語ファイル名をパスに使わない)。"""
    return project_dir(workspace_dir, project_id) / f"source.{audio_format}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


def write_stage_metadata(
    workspace_dir: Path,
    project_id: str,
    stage: str,
    *,
    params: dict,
    provider_versions: dict,
) -> None:
    """NFR-11: 実行パラメータ・使用モデル・プロバイダのバージョンを成果物メタデータに記録する。"""
    meta_path = project_dir(workspace_dir, project_id) / "analysis" / f"{stage}.meta.json"
    write_json(meta_path, {"stage": stage, "params": params, "versions": provider_versions})


def delete_project_dir(workspace_dir: Path, project_id: str) -> None:
    import shutil

    base = project_dir(workspace_dir, project_id)
    if base.exists():
        shutil.rmtree(base)
