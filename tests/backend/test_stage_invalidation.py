"""#29: ステージ依存グラフ・無効化のテスト(FR-14)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.stages import downstream_of
from app.infra import storage
from app.services.stage_invalidation import invalidate_downstream


class TestDownstreamOf:
    def test_separate_invalidates_transcribe_and_transitively_quantize(self) -> None:
        assert downstream_of("separate") == ("transcribe", "quantize")

    def test_beat_invalidates_quantize(self) -> None:
        assert downstream_of("beat") == ("quantize",)

    def test_transcribe_invalidates_quantize(self) -> None:
        assert downstream_of("transcribe") == ("quantize",)

    def test_quantize_has_no_downstream(self) -> None:
        """quantizeより下流(exportは同期エンドポイントでmeta.jsonを持たない)は無い。"""
        assert downstream_of("quantize") == ()

    def test_unknown_stage_has_no_downstream(self) -> None:
        assert downstream_of("dummy") == ()


class TestInvalidateDownstream:
    def _write_meta(self, workspace_dir: Path, project_id: str, stage: str) -> None:
        storage.write_stage_metadata(
            workspace_dir, project_id, stage, params_hash="h", provider_versions={}
        )

    def test_deletes_meta_json_for_all_downstream_stages(self, tmp_path: Path) -> None:
        project_id = "proj_test"
        storage.ensure_project_layout(tmp_path, project_id)
        for stage in ("separate", "transcribe", "quantize"):
            self._write_meta(tmp_path, project_id, stage)

        invalidated = invalidate_downstream(tmp_path, project_id, "separate")

        assert set(invalidated) == {"transcribe", "quantize"}
        assert storage.stage_metadata_path(tmp_path, project_id, "separate").exists()
        assert not storage.stage_metadata_path(
            tmp_path, project_id, "transcribe"
        ).exists()
        assert not storage.stage_metadata_path(
            tmp_path, project_id, "quantize"
        ).exists()

    def test_beat_invalidation_only_touches_quantize(self, tmp_path: Path) -> None:
        project_id = "proj_test"
        storage.ensure_project_layout(tmp_path, project_id)
        for stage in ("separate", "transcribe", "quantize"):
            self._write_meta(tmp_path, project_id, stage)

        invalidated = invalidate_downstream(tmp_path, project_id, "beat")

        assert invalidated == ["quantize"]
        assert storage.stage_metadata_path(tmp_path, project_id, "separate").exists()
        assert storage.stage_metadata_path(tmp_path, project_id, "transcribe").exists()
        assert not storage.stage_metadata_path(
            tmp_path, project_id, "quantize"
        ).exists()

    def test_missing_downstream_meta_is_skipped_without_error(
        self, tmp_path: Path
    ) -> None:
        """下流ステージが未実行(meta.json無し)でも例外にならない。"""
        project_id = "proj_test"
        storage.ensure_project_layout(tmp_path, project_id)
        self._write_meta(tmp_path, project_id, "separate")

        invalidated = invalidate_downstream(tmp_path, project_id, "separate")

        assert invalidated == []

    def test_retries_on_permission_error_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回帰(#29-M2レビュー指摘): Windowsで他プロセスがmeta.jsonを開いている

        間の削除は`PermissionError`(WinError 32)になりうる。短いリトライで
        回避できることを確認する(実際のファイルロックは環境依存なので、
        `Path.unlink`をモックして再現する)。
        """
        # "beat"の下流は"quantize"のみ(#25/#26参照)なので、unlinkの呼び出し
        # 回数をこの1ファイル分だけに絞って厳密に検証できる。
        project_id = "proj_test"
        storage.ensure_project_layout(tmp_path, project_id)
        self._write_meta(tmp_path, project_id, "quantize")

        original_unlink = Path.unlink
        call_count = 0

        def _flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise PermissionError("simulated Windows sharing violation")
            original_unlink(self)

        monkeypatch.setattr(Path, "unlink", _flaky_unlink)

        invalidated = invalidate_downstream(tmp_path, project_id, "beat")

        assert invalidated == ["quantize"]
        assert call_count == 3

    def test_gives_up_after_repeated_permission_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """リトライ上限を超えて`PermissionError`が続く場合は、無言で無視せず送出する

        (fail-closed: 無効化されないまま古いmeta.jsonが残ることを検出できるように)。
        """
        project_id = "proj_test"
        storage.ensure_project_layout(tmp_path, project_id)
        self._write_meta(tmp_path, project_id, "transcribe")

        def _always_locked(self: Path, *args: object, **kwargs: object) -> None:
            raise PermissionError("simulated Windows sharing violation")

        monkeypatch.setattr(Path, "unlink", _always_locked)

        with pytest.raises(PermissionError):
            invalidate_downstream(tmp_path, project_id, "separate")
