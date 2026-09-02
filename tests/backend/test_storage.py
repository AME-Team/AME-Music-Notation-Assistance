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


def test_should_skip_stage_true_when_hash_matches_and_no_artifact_check(
    tmp_path: Path,
) -> None:
    storage.ensure_project_layout(tmp_path, "proj_a")
    storage.write_stage_metadata(
        tmp_path, "proj_a", "separate", params_hash="h1", provider_versions={}
    )

    assert storage.should_skip_stage(tmp_path, "proj_a", "separate", "h1") is True


def test_should_skip_stage_false_when_hash_matches_but_artifacts_missing(
    tmp_path: Path,
) -> None:
    """回帰(#21-M1レビュー指摘): ハッシュが一致しても成果物が存在しなければスキップしない。

    ジョブが強制終了され `except BaseException` クリーンアップが走らなかった場合や、
    ステム/beatmap.jsonを手動削除した場合に、旧メタデータだけを根拠にスキップして
    破損/欠落した成果物を使い回してしまうのを防ぐ。
    """
    storage.ensure_project_layout(tmp_path, "proj_a")
    storage.write_stage_metadata(
        tmp_path, "proj_a", "separate", params_hash="h1", provider_versions={}
    )

    assert (
        storage.should_skip_stage(
            tmp_path, "proj_a", "separate", "h1", artifacts_exist=lambda meta: False
        )
        is False
    )


def test_should_skip_stage_false_when_hash_differs_regardless_of_artifacts(
    tmp_path: Path,
) -> None:
    storage.ensure_project_layout(tmp_path, "proj_a")
    storage.write_stage_metadata(
        tmp_path, "proj_a", "separate", params_hash="h1", provider_versions={}
    )

    assert (
        storage.should_skip_stage(
            tmp_path, "proj_a", "separate", "h2", artifacts_exist=lambda meta: True
        )
        is False
    )


def test_should_skip_stage_passes_recorded_artifact_names_to_callback(
    tmp_path: Path,
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): artifacts_existコールバックは

    write_stage_metadataで記録した artifact_names を meta 経由で受け取れる
    べき(部分削除を検出するために、呼び出し元が期待成果物名と現在のディスク
    状態を比較できる必要がある)。
    """
    storage.ensure_project_layout(tmp_path, "proj_a")
    storage.write_stage_metadata(
        tmp_path,
        "proj_a",
        "separate",
        params_hash="h1",
        provider_versions={},
        artifact_names=["drums", "bass"],
    )

    received: list = []

    def _check(meta: dict) -> bool:
        received.append(meta.get("artifact_names"))
        return True

    storage.should_skip_stage(
        tmp_path, "proj_a", "separate", "h1", artifacts_exist=_check
    )
    assert received == [["drums", "bass"]]


def test_should_skip_stage_false_when_artifacts_exist_raises_oserror(
    tmp_path: Path,
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): should_skip_stageは、メタデータ

    読み取り不能なら再実行させるfail-safe設計(既存のJSONDecodeError/OSError
    処理と同じ)。artifacts_existコールバックがディスクI/OでOSErrorを送出した
    場合も、ジョブ全体を未処理例外でクラッシュさせず、同様にFalse(再実行)へ
    フォールバックするべき。
    """
    storage.ensure_project_layout(tmp_path, "proj_a")
    storage.write_stage_metadata(
        tmp_path, "proj_a", "separate", params_hash="h1", provider_versions={}
    )

    def _raising_check(meta: dict) -> bool:
        raise OSError("simulated disk I/O failure")

    assert (
        storage.should_skip_stage(
            tmp_path, "proj_a", "separate", "h1", artifacts_exist=_raising_check
        )
        is False
    )


def test_should_skip_stage_false_when_artifact_names_is_malformed(
    tmp_path: Path,
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): メタデータのartifact_namesがJSONと

    しては有効でも非イテラブルな不正値(例: 数値)だと、呼び出し元のコールバックが
    `set(...)` へ通した際にTypeErrorを送出しうる。これもfail-safeにFalse
    (再実行)へフォールバックし、ジョブ全体をクラッシュさせないべき。
    """
    storage.ensure_project_layout(tmp_path, "proj_a")
    storage.write_stage_metadata(
        tmp_path, "proj_a", "separate", params_hash="h1", provider_versions={}
    )
    meta_path = storage.stage_metadata_path(tmp_path, "proj_a", "separate")
    meta = storage.read_json(meta_path)
    meta["artifact_names"] = 42  # 不正な形式(list[str]であるべき)
    storage.write_json(meta_path, meta)

    def _check(meta: dict) -> bool:
        return set(meta["artifact_names"]) <= set()  # 数値をsetに通すとTypeError

    assert (
        storage.should_skip_stage(
            tmp_path, "proj_a", "separate", "h1", artifacts_exist=_check
        )
        is False
    )
