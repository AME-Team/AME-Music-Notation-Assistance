"""#65 FR-18: プロジェクトの単一アーカイブ書き出し/読み込み。"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.infra import storage
from app.services.project_archive_service import (
    ARCHIVE_SCHEMA_VERSION,
    InvalidArchiveError,
    export_project_archive,
    import_project_archive,
)
from app.services.project_service import ProjectService
from fastapi.testclient import TestClient


def _insert_job(
    service: ProjectService, project_id: str, *, job_id: str = "job_x"
) -> None:
    now = datetime.now(UTC).isoformat()
    conn = service._conn()
    conn.execute(
        "INSERT INTO jobs(id, project_id, stage, status, progress, message, params_json, "
        "exit_code, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, project_id, "quantize", "succeeded", 1.0, None, "{}", 0, now, now),
    )
    conn.commit()


def _insert_agent_run(
    service: ProjectService, project_id: str, *, run_id: str = "run_x"
) -> None:
    now = datetime.now(UTC).isoformat()
    conn = service._conn()
    conn.execute(
        "INSERT INTO agent_runs(id, project_id, status, turns, usage_json, staged_ops_count, "
        "error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, project_id, "succeeded", 3, "{}", 2, None, now),
    )
    conn.commit()


def _insert_revision(
    service: ProjectService, project_id: str, *, rev_id: str = "rev_x"
) -> None:
    now = datetime.now(UTC).isoformat()
    conn = service._conn()
    snapshot = json.dumps({"schema_version": 3, "project_id": project_id, "parts": []})
    conn.execute(
        "INSERT INTO revisions(id, project_id, name, description, op_count, score_snapshot, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rev_id, project_id, "checkpoint 1", "desc", 5, snapshot, now),
    )
    conn.commit()


def _make_project(
    workspace_dir: Path, tiny_wav_bytes: bytes, *, with_stem: bool = True
) -> tuple[ProjectService, str]:
    service = ProjectService(workspace_dir=workspace_dir)
    project = service.create_project(
        original_filename="song.wav", content=tiny_wav_bytes
    )
    project_id = project["id"]

    storage.write_json(
        storage.score_current_path(workspace_dir, project_id),
        {"schema_version": 3, "project_id": project_id, "parts": []},
    )
    storage.write_json(
        storage.score_staging_path(workspace_dir, project_id, "run_x"),
        {"schema_version": 3, "project_id": project_id, "parts": [{"id": "staged"}]},
    )
    if with_stem:
        stems_dir = storage.stems_dir(workspace_dir, project_id)
        stems_dir.mkdir(parents=True, exist_ok=True)
        (stems_dir / "piano.wav").write_bytes(b"RIFF-fake-wav-data")

    _insert_job(service, project_id)
    _insert_agent_run(service, project_id)
    _insert_revision(service, project_id)

    # #65: agent run の展開先ディレクトリ名(run_id)が読み込み時に張り替わることを
    # 検証するため、実際にそのディレクトリへファイルを置いておく。
    agent_dir = storage.agent_workspace_dir(workspace_dir, project_id, "run_x")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "TASK.md").write_text("do the thing", encoding="utf-8")

    return service, project_id


class TestExportImportRoundTrip:
    def test_round_trip_creates_a_new_project_with_the_same_content(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)

        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=True
        )
        assert archive_path.exists()

        imported = import_project_archive(
            workspace_dir, service, archive_path.read_bytes()
        )

        assert imported["id"] != project_id
        assert imported["original_filename"] == "song.wav"
        assert imported["audio_format"] == "wav"

        new_id = imported["id"]
        assert storage.original_audio_path(workspace_dir, new_id, "wav").exists()
        assert storage.score_current_path(workspace_dir, new_id).exists()
        assert (
            storage.stems_dir(workspace_dir, new_id) / "piano.wav"
        ).read_bytes() == (b"RIFF-fake-wav-data")

    def test_embedded_project_id_is_rewritten_to_the_new_id(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        """#65レビュー指摘(HIGH): ファイル内容やscore_snapshotが持つ

        `project_id`も新IDへ書き換わる(DB行のIDだけ張り替えて内容は
        旧プロジェクトを指したまま、という不整合を防ぐ)。
        """
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        )
        imported = import_project_archive(
            workspace_dir, service, archive_path.read_bytes()
        )
        new_id = imported["id"]
        assert new_id != project_id

        current_score = storage.read_json(
            storage.score_current_path(workspace_dir, new_id)
        )
        assert current_score["project_id"] == new_id

        conn = service._conn()
        new_run_id = conn.execute(
            "SELECT id FROM agent_runs WHERE project_id = ?", (new_id,)
        ).fetchone()["id"]

        # ステージングファイル名(run_idを含む)も新run_idへ付け替わっている
        # (#65レビュー指摘、HIGH: 最初は内容だけ書き換え、ファイル名が旧
        # run_idのまま残っていた)。
        staged_path = storage.score_staging_path(workspace_dir, new_id, new_run_id)
        assert staged_path.exists()
        staged = storage.read_json(staged_path)
        assert staged["project_id"] == new_id

        rev = conn.execute(
            "SELECT score_snapshot FROM revisions WHERE project_id = ?", (new_id,)
        ).fetchone()
        assert json.loads(rev["score_snapshot"])["project_id"] == new_id

    def test_db_rows_are_reinserted_under_new_ids(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        )
        imported = import_project_archive(
            workspace_dir, service, archive_path.read_bytes()
        )
        new_id = imported["id"]

        conn = service._conn()
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ?", (new_id,)
        ).fetchall()
        agent_runs = conn.execute(
            "SELECT * FROM agent_runs WHERE project_id = ?", (new_id,)
        ).fetchall()
        revisions = conn.execute(
            "SELECT * FROM revisions WHERE project_id = ?", (new_id,)
        ).fetchall()

        assert len(jobs) == 1
        assert jobs[0]["stage"] == "quantize"
        assert jobs[0]["id"] != "job_x"  # 新規採番されている

        assert len(agent_runs) == 1
        assert agent_runs[0]["status"] == "succeeded"
        new_run_id = agent_runs[0]["id"]
        assert new_run_id != "run_x"

        assert len(revisions) == 1
        assert revisions[0]["name"] == "checkpoint 1"
        assert revisions[0]["id"] != "rev_x"

        # agent run のディレクトリ名も新IDへ付け替わっている。
        assert not storage.agent_workspace_dir(workspace_dir, new_id, "run_x").exists()
        new_agent_dir = storage.agent_workspace_dir(workspace_dir, new_id, new_run_id)
        assert (new_agent_dir / "TASK.md").read_text(encoding="utf-8") == "do the thing"

        # 元プロジェクトのDB行はそのまま残っている(インポートは新規作成、上書きではない)。
        original_jobs = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ?", (project_id,)
        ).fetchall()
        assert len(original_jobs) == 1
        assert original_jobs[0]["id"] == "job_x"

    def test_importing_the_same_archive_twice_does_not_collide(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        """同じアーカイブを2回読み込んでもDB行ID(job/agent_run/revision)の

        衝突が起きない(#65設計: 読み込みのたびに新規採番するため)。
        """
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        archive_bytes = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        ).read_bytes()

        first = import_project_archive(workspace_dir, service, archive_bytes)
        second = import_project_archive(workspace_dir, service, archive_bytes)

        assert first["id"] != second["id"]

    def test_include_stems_false_excludes_stem_wav_files(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        )
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
        assert not any(name.endswith("stems/piano.wav") for name in names)
        # ステム以外(原曲・スコア等)は引き続き含まれる。
        assert any(name.endswith("source.wav") for name in names)
        assert any(name.endswith("score/current.json") for name in names)

        manifest = json.loads(zipfile.ZipFile(archive_path).read("manifest.json"))
        assert manifest["includes_stems"] is False

    def test_include_stems_false_excludes_wav_files_in_stems_subdirectories_too(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        """#65レビュー指摘(LOW): `stems/`直下だけでなく、サブディレクトリ

        構成になった場合でも配下の`.wav`は除外する。
        """
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        nested = storage.stems_dir(workspace_dir, project_id) / "variant"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "piano_v2.wav").write_bytes(b"nested-stem")

        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        )
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
        assert not any(name.endswith("piano_v2.wav") for name in names)

    def test_include_stems_true_includes_stem_wav_files(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=True
        )
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
        assert any(name.endswith("stems/piano.wav") for name in names)

    def test_reexporting_does_not_embed_the_previous_archive_into_itself(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        export_project_archive(workspace_dir, project_id, service, include_stems=False)
        second_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        )
        with zipfile.ZipFile(second_path) as zf:
            names = zf.namelist()
        assert not any(name.endswith("archive.ameproj") for name in names)

    def test_a_leftover_tmp_file_from_a_crashed_export_is_not_embedded(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        """#65レビュー指摘(LOW): 直前のエクスポートがプロセス強制終了等で

        異常終了して`archive.ameproj.tmp`が残留していても、次回のエクスポート
        に混入しない。
        """
        service, project_id = _make_project(workspace_dir, tiny_wav_bytes)
        leftover_tmp = storage.project_archive_path(
            workspace_dir, project_id
        ).with_name("archive.ameproj.tmp")
        leftover_tmp.write_bytes(b"partial zip from a crashed run")

        archive_path = export_project_archive(
            workspace_dir, project_id, service, include_stems=False
        )
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
        assert not any(name.endswith(".tmp") for name in names)


class TestImportValidation:
    def test_not_a_zip_file_raises(
        self, workspace_dir: Path, tiny_wav_bytes: bytes
    ) -> None:
        service = ProjectService(workspace_dir=workspace_dir)
        with pytest.raises(InvalidArchiveError, match="not a valid zip"):
            import_project_archive(workspace_dir, service, b"not a zip file")

    def test_missing_manifest_raises(self, workspace_dir: Path) -> None:
        service = ProjectService(workspace_dir=workspace_dir)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("project/source.wav", b"x")
        with pytest.raises(InvalidArchiveError, match="manifest.json"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_future_archive_schema_version_is_rejected(
        self, workspace_dir: Path
    ) -> None:
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION + 1,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="archive_schema_version"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_future_score_schema_version_is_rejected(self, workspace_dir: Path) -> None:
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "score_schema_version": 999,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="schema_version"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_future_db_schema_version_is_rejected(self, workspace_dir: Path) -> None:
        """#65レビュー指摘(MIDDLE): db_schema_versionも未来バージョンを拒否する。"""
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "db_schema_version": 999,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="db_schema_version"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_missing_project_metadata_is_rejected(self, workspace_dir: Path) -> None:
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {"archive_schema_version": ARCHIVE_SCHEMA_VERSION}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="project"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_zip_slip_path_traversal_member_is_rejected(
        self, workspace_dir: Path
    ) -> None:
        """#65: 細工されたアーカイブが展開先ディレクトリの外へ書き込もうとするのを拒否する。"""
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("project/../../../../tmp/evil.txt", b"pwned")

        with pytest.raises(InvalidArchiveError, match="escapes"):
            import_project_archive(workspace_dir, service, buf.getvalue())

        # 失敗時にプロジェクトディレクトリ(proj_*)が残らない(後始末される)ことを
        # 確認する(`db.sqlite3`自体はコネクション初期化の副作用として作られうる
        # ため、workspace_dir配下の差分をそのまま比較はしない)。
        assert list(workspace_dir.glob("proj_*")) == []

    def test_malformed_job_row_is_rejected(self, workspace_dir: Path) -> None:
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            "jobs": [{"id": "job_1"}],  # stage/status/created_at/updated_at 欠落
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="jobs"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_corrupted_zip_member_is_rejected_as_422_not_500(
        self, workspace_dir: Path
    ) -> None:
        """#65レビュー指摘(MIDDLE): CRC不一致のメンバーは`InvalidArchiveError`

        (呼び出し元で422)になる。以前は`zipfile.BadZipFile`がそのまま
        伝播し、API層で未処理例外(500)になっていた。
        """
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("project/source.wav", b"not corrupted yet")

        raw = bytearray(buf.getvalue())
        with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
            info = zf.getinfo("project/source.wav")
        name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
        data_offset = info.header_offset + 30 + name_len + extra_len
        raw[data_offset] ^= 0xFF  # 1バイト反転してCRC不一致を起こす

        with pytest.raises(InvalidArchiveError, match="source.wav"):
            import_project_archive(workspace_dir, service, bytes(raw))

    def test_archive_with_too_many_members_is_rejected(
        self, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#65レビュー指摘(LOW): zip爆弾対策の上限を実際に検証する

        (実際に200,000件超のzipを作るのは重いため、上限を小さくして確認する)。
        """
        import app.services.project_archive_service as archive_service

        monkeypatch.setattr(archive_service, "_MAX_ARCHIVE_MEMBERS", 3)
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            for i in range(4):
                zf.writestr(f"project/extra_{i}.txt", "x")
        with pytest.raises(InvalidArchiveError, match="too many entries"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_non_string_score_snapshot_is_rejected_as_422_not_500(
        self, workspace_dir: Path
    ) -> None:
        """#65レビュー指摘(MIDDLE): 型検証が無いと`score_snapshot`が文字列以外

        (例: JSONオブジェクト)でも`_validate_manifest`を通過し、後段の
        `json.loads`が`TypeError`を送出して未処理例外(500)になっていた。
        """
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
            },
            "revisions": [
                {
                    "id": "rev_1",
                    "name": "r1",
                    "score_snapshot": {"not": "a string"},
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="non-string"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_non_scalar_optional_field_is_rejected_as_422_not_500(
        self, workspace_dir: Path
    ) -> None:
        """#65レビュー指摘(2巡目、MIDDLE): 必須キーだけでなく任意キー

        (例: agent_runsのusage_json)も、dict/listが混入すると
        `sqlite3.InterfaceError`(未処理例外、500)になっていた。
        """
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
            },
            "agent_runs": [
                {
                    "id": "run_1",
                    "status": "succeeded",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "usage_json": {"tokens": 100},  # 本来はJSON文字列であるべき
                }
            ],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(InvalidArchiveError, match="non-scalar"):
            import_project_archive(workspace_dir, service, buf.getvalue())

    def test_created_at_is_not_required_in_the_manifest(
        self, workspace_dir: Path
    ) -> None:
        """#65レビュー指摘(LOW): 読み込みは常に新規作成で元のcreated_atを

        使わないため、manifestに無くても読み込める。
        """
        service = ProjectService(workspace_dir=workspace_dir)
        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "project": {
                "name": "x",
                "original_filename": "x.wav",
                "audio_format": "wav",
                # created_at を意図的に省略
            },
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        imported = import_project_archive(workspace_dir, service, buf.getvalue())
        assert imported["original_filename"] == "x.wav"


class TestArchiveApi:
    def test_export_then_import_via_http(
        self, client: TestClient, tiny_wav_bytes: bytes
    ) -> None:
        resp = client.post(
            "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
        )
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["id"]

        resp = client.get(f"/api/projects/{project_id}/archive")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/zip"
        archive_bytes = resp.content

        resp = client.post(
            "/api/projects/import",
            files={"file": ("song.ameproj", archive_bytes, "application/zip")},
        )
        assert resp.status_code == 201, resp.text
        imported = resp.json()
        assert imported["id"] != project_id
        assert imported["original_filename"] == "song.wav"

    def test_archive_of_unknown_project_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/projects/proj_doesnotexist/archive")
        assert resp.status_code == 404

    def test_import_of_invalid_archive_is_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/projects/import",
            files={"file": ("bad.ameproj", b"not a zip", "application/zip")},
        )
        assert resp.status_code == 422
