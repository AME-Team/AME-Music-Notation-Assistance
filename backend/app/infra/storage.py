"""workspace/{project_id}/ のレイアウト操作(#12 §10.3)。

#79 Windows専用化の方針:
- パス結合は必ず pathlib.Path を使い、文字列連結しない。
- 全ファイル I/O は encoding="utf-8" を明示する。
- 生成する .json / .jsonl は改行コードを LF に統一する(newline="\\n")。
"""

from __future__ import annotations

import json
import os
import tempfile
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


def find_original_audio(workspace_dir: Path, project_id: str) -> Path:
    """拡張子を問わず `source.*` を探す(#16/#18: DSP Worker はDBを見ずファイルだけで完結する)。

    1プロジェクト1音源が前提(現状に再アップロード経路は無い)。複数マッチした場合、
    どちらを使うべきかWorker側では判断できないため、黙って先頭を選ばず例外にする。
    """
    matches = sorted(project_dir(workspace_dir, project_id).glob("source.*"))
    if not matches:
        raise FileNotFoundError(f"original audio not found for project {project_id!r}")
    if len(matches) > 1:
        raise RuntimeError(
            f"multiple source audio files found for project {project_id!r}: {matches} "
            "(expected exactly one; this indicates a data inconsistency)"
        )
    return matches[0]


def stems_dir(workspace_dir: Path, project_id: str) -> Path:
    return project_dir(workspace_dir, project_id) / "stems"


def list_stem_names(workspace_dir: Path, project_id: str) -> set[str]:
    """現在ディスクにあるステム名の集合(拡張子抜き)。プリセット変更時の差分検出に使う。"""
    return {p.stem for p in stems_dir(workspace_dir, project_id).glob("*.wav")}


def beatmap_path(workspace_dir: Path, project_id: str) -> Path:
    return project_dir(workspace_dir, project_id) / "analysis" / "beatmap.json"


def peaks_path(workspace_dir: Path, project_id: str, name: str) -> Path:
    return project_dir(workspace_dir, project_id) / "analysis" / "peaks" / f"{name}.json"


def invalidate_peaks_cache(workspace_dir: Path, project_id: str, names: list[str]) -> None:
    """再分離でステムが上書きされた際、古い波形ピークキャッシュを消す。

    `get_peaks`(api/media.py)はキャッシュが存在する限り再計算しないため、これを
    怠るとプリセット/EPを変えて再分離しても古いステムの波形が表示され続ける。
    """
    for name in names:
        peaks_path(workspace_dir, project_id, name).unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    """JSONをアトミックに書き込む。

    DSP Worker(別プロセス)がbeatmap.json等を書いている最中に、API サーバが同じ
    ファイルへのGETを同時に処理しうる(#16レビュー指摘と同種のレース)。同一
    ディレクトリ内の一時ファイルに書いてから `os.replace`(POSIX/Windowsともに
    アトミック)でリネームすることで、読み取り側は書き込み前後どちらかの完全な
    状態しか observe しない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


def stage_metadata_path(workspace_dir: Path, project_id: str, stage: str) -> Path:
    return project_dir(workspace_dir, project_id) / "analysis" / f"{stage}.meta.json"


def write_stage_metadata(
    workspace_dir: Path,
    project_id: str,
    stage: str,
    *,
    params_hash: str,
    provider_versions: dict,
) -> None:
    """NFR-11: 実行パラメータ・使用モデル・プロバイダのバージョンを成果物メタデータに記録する。

    §6: パラメータのハッシュも記録し、入力・パラメータ不変ならスキップできるようにする。
    """
    write_json(
        stage_metadata_path(workspace_dir, project_id, stage),
        {"stage": stage, "params_hash": params_hash, "versions": provider_versions},
    )


def should_skip_stage(workspace_dir: Path, project_id: str, stage: str, params_hash: str) -> bool:
    """§6: 直前の実行と同じパラメータハッシュなら再実行をスキップしてよいと判定する。"""
    meta_path = stage_metadata_path(workspace_dir, project_id, stage)
    if not meta_path.exists():
        return False
    try:
        meta = read_json(meta_path)
    except (json.JSONDecodeError, OSError):
        return False
    return meta.get("params_hash") == params_hash


def delete_project_dir(workspace_dir: Path, project_id: str) -> None:
    import shutil

    base = project_dir(workspace_dir, project_id)
    if base.exists():
        shutil.rmtree(base)
