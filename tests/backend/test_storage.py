"""infra/storage.py の単体テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infra import storage


def test_find_original_audio_returns_the_single_match(tmp_path: Path) -> None:
    storage.ensure_project_layout(tmp_path, "proj_a")
    path = tmp_path / "proj_a" / "source.wav"
    path.write_bytes(b"data")

    assert storage.find_original_audio(tmp_path, "proj_a") == path


def test_find_original_audio_missing_raises_file_not_found(tmp_path: Path) -> None:
    storage.ensure_project_layout(tmp_path, "proj_a")
    with pytest.raises(FileNotFoundError):
        storage.find_original_audio(tmp_path, "proj_a")


def test_find_original_audio_multiple_matches_raises_instead_of_guessing(
    tmp_path: Path,
) -> None:
    """#16レビュー指摘: 複数マッチ時に黙って先頭を選ぶとDBのaudio_formatと食い違いうる。"""
    storage.ensure_project_layout(tmp_path, "proj_a")
    (tmp_path / "proj_a" / "source.wav").write_bytes(b"data")
    (tmp_path / "proj_a" / "source.mp3").write_bytes(b"data")

    with pytest.raises(RuntimeError, match="multiple source audio files"):
        storage.find_original_audio(tmp_path, "proj_a")
