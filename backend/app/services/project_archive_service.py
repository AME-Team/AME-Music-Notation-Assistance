"""プロジェクトの単一アーカイブ書き出し/読み込み(#65, FR-18, §10.3, §15 R-8)。

アーカイブは zip 形式(拡張子 `.ameproj`)で、以下を含む:

- `manifest.json`: プロジェクトメタデータ(`projects`テーブルの1行) +
  `jobs`/`agent_runs`/`revisions`テーブルの該当行(いずれも`project_id`で絞り込み) +
  スキーマバージョン情報。Score IR本体はSQLiteの行に分解しない設計(§10.3)のため、
  DBの行としてはこの3テーブルのみが対象になる。
- `project/` 以下: `workspace/{project_id}/` の全ファイル(`stems/*.wav`は
  `include_stems=False`なら除外、サイズの大半を占めるため)。

読み込み時は`project_id`を含む全てのDB行IDを新規採番する(#65設計判断: 同じ
アーカイブを同一ワークスペースへ複数回読み込んでも、あるいは既存プロジェクトの
IDと衝突しても安全なように、`ids.new_id()`で完全に新しいIDへ張り替える。
`create_project`が常に新規`project_id`を発行するのと同じ考え方)。
`agent_runs`のIDは`workspace/{project_id}/agent/{run_id}/`のパスに使われて
いるため、DB行のIDを張り替えると同時に展開先のディレクトリ名も付け替える。
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.score import CURRENT_SCHEMA_VERSION as CURRENT_SCORE_SCHEMA_VERSION
from app.infra import db, ids, storage
from app.services.project_service import ProjectService

ARCHIVE_SCHEMA_VERSION = 1

_MANIFEST_NAME = "manifest.json"
_MEMBER_PREFIX = "project/"

_JOB_REQUIRED_KEYS = ("id", "stage", "status", "created_at", "updated_at")
_AGENT_RUN_REQUIRED_KEYS = ("id", "status", "created_at")
_REVISION_REQUIRED_KEYS = ("id", "name", "score_snapshot", "created_at")

# #65レビュー指摘(LOW): exportは`db.py`の`jobs`/`agent_runs`/`revisions`の
# 全列を明示的に列挙する(`SELECT *`にしない)。`SELECT *`だと、将来テーブルに
# 列が追加された際に import側の固定INSERT列リストと**気づかないまま**
# 乖離し、新しい値が黙って欠落する(NOT NULL制約付きなら例外)。
# `archive_schema_version`のガードも列単位の乖離までは検出できないため、
# ここを列挙にしておけば、列を追加する変更者がこのファイルも同時に
# 更新する必要に気づける。
_JOB_COLUMNS = (
    "id",
    "stage",
    "status",
    "progress",
    "message",
    "params_json",
    "exit_code",
    "created_at",
    "updated_at",
)
_AGENT_RUN_COLUMNS = (
    "id",
    "status",
    "turns",
    "usage_json",
    "staged_ops_count",
    "error",
    "created_at",
)
_REVISION_COLUMNS = ("id", "name", "description", "op_count", "score_snapshot", "created_at")

# #65レビュー指摘(LOW): 細工されたzip(zip bomb)でメモリ・ディスクを
# 枯渇させられないよう、展開前にzipの中央ディレクトリ(メタデータのみ、
# 実体は読まない)から合計サイズ・件数を見積もって上限を掛ける。単一
# ユーザーのローカルデスクトップアプリで、ユーザー自身がファイル選択
# ダイアログで選んだファイルが対象のため脅威度は低いが、コストが低いので
# 防御として追加する。値は現実的なステム付きアーカイブ(実測: 6ステム×
# 数分の曲で数百MB)に余裕を持たせた目安。
_MAX_ARCHIVE_MEMBERS = 200_000
_MAX_UNCOMPRESSED_BYTES = 20 * 1024**3  # 20 GiB


class InvalidArchiveError(ValueError):
    """アーカイブの形式が不正、または未対応のバージョン(呼び出し元は422相当に変換すること)。"""


def export_project_archive(
    workspace_dir: Path, project_id: str, service: ProjectService, *, include_stems: bool
) -> Path:
    """プロジェクトを単一アーカイブへ書き出し、生成したファイルのパスを返す。

    `service.get_project`が投げる`ProjectNotFoundError`はそのまま呼び出し元へ伝播する
    (`export_score`と同じ方針、API層で404に変換する)。
    """
    project = service.get_project(project_id)
    conn = db.get_connection(workspace_dir / "db.sqlite3")
    # `db._row_factory` が常に dict を返すため、ここでの追加ラップは不要。
    # 列は明示的に列挙する(`SELECT *`にしない、モジュール定数のdocstring参照)。
    # f-stringでSQLを組み立てここでS608を無効化するのは、このコードベースの
    # 既存パターン(外部入力ではなくモジュール定数の列名のみを埋め込む、
    # `agent_run_service.py`のUPDATE/SELECT、`db.py`のALTER TABLEと同じ)。
    jobs = conn.execute(
        f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE project_id = ? ORDER BY created_at",  # noqa: S608
        (project_id,),
    ).fetchall()
    agent_runs = conn.execute(
        f"SELECT {', '.join(_AGENT_RUN_COLUMNS)} FROM agent_runs "  # noqa: S608
        "WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    revisions = conn.execute(
        f"SELECT {', '.join(_REVISION_COLUMNS)} FROM revisions "  # noqa: S608
        "WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    score_schema_version = None
    score_path = storage.score_current_path(workspace_dir, project_id)
    if score_path.exists():
        score_schema_version = storage.read_json(score_path).get("schema_version")

    manifest: dict[str, Any] = {
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "db_schema_version": db.SCHEMA_VERSION,
        "score_schema_version": score_schema_version,
        "includes_stems": include_stems,
        "project": {
            "name": project["name"],
            "original_filename": project["original_filename"],
            "audio_format": project["audio_format"],
            "created_at": project["created_at"],
        },
        "jobs": jobs,
        "agent_runs": agent_runs,
        "revisions": revisions,
    }

    archive_path = storage.project_archive_path(workspace_dir, project_id)
    project_root = storage.project_dir(workspace_dir, project_id)
    stems_root = storage.stems_dir(workspace_dir, project_id)
    tmp_path = archive_path.with_name(archive_path.name + ".tmp")

    # #65レビュー教訓: ファイル一覧は zip を開く**前**に確定させる。zip書き込み先
    # (`tmp_path`)は`project_root`配下(`export/`)にあるため、先に`ZipFile`を
    # 開いてから`rglob`すると、書き込み中のアーカイブ自身が一覧に混入しうる。
    # `tmp_path`自体も除外する(レビュー指摘、LOW: 直前の書き出しがプロセス
    # 強制終了等で異常終了すると`archive.ameproj.tmp`が残留しうるため)。
    files = [
        path
        for path in sorted(project_root.rglob("*"))
        if path.is_file() and path not in (archive_path, tmp_path)
    ]
    if not include_stems:
        # レビュー指摘(LOW): `path.parent != stems_root`だと`stems/`直下しか
        # 除外できず、将来サブディレクトリ構成になった場合に取りこぼす。
        # `stems_root`配下全体(サブディレクトリを含む)の`.wav`を対象にする。
        files = [path for path in files if stems_root not in path.parents or path.suffix != ".wav"]

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            for path in files:
                arcname = _MEMBER_PREFIX + path.relative_to(project_root).as_posix()
                zf.write(path, arcname)
        tmp_path.replace(archive_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return archive_path


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise InvalidArchiveError(f"{_MANIFEST_NAME} must be a JSON object")

    archive_version = manifest.get("archive_schema_version")
    if not isinstance(archive_version, int):
        raise InvalidArchiveError(f"{_MANIFEST_NAME} is missing a valid archive_schema_version")
    if archive_version > ARCHIVE_SCHEMA_VERSION:
        raise InvalidArchiveError(
            f"archive_schema_version {archive_version} is newer than this build supports "
            f"({ARCHIVE_SCHEMA_VERSION}); refusing to import"
        )

    score_schema_version = manifest.get("score_schema_version")
    if (
        isinstance(score_schema_version, int)
        and score_schema_version > CURRENT_SCORE_SCHEMA_VERSION
    ):
        raise InvalidArchiveError(
            f"the archived score's schema_version ({score_schema_version}) is newer than this "
            f"build supports ({CURRENT_SCORE_SCHEMA_VERSION}); refusing to import"
        )

    db_schema_version = manifest.get("db_schema_version")
    if isinstance(db_schema_version, int) and db_schema_version > db.SCHEMA_VERSION:
        raise InvalidArchiveError(
            f"the archive's db_schema_version ({db_schema_version}) is newer than this build "
            f"supports ({db.SCHEMA_VERSION}); refusing to import"
        )

    project_row = manifest.get("project")
    if not isinstance(project_row, dict):
        raise InvalidArchiveError(f"{_MANIFEST_NAME} is missing 'project'")
    # `created_at`は必須にしない(レビュー指摘、LOW): 読み込みは常に新規作成
    # であり(モジュールdocstring参照)、`import_project_archive`は元の
    # `created_at`を使わず`datetime.now(UTC)`で採番し直す。manifestには
    # 記録目的(いつエクスポートされた元プロジェクトか)で残すが、無くても
    # 読み込みは成立するため必須検証の対象からは外す。
    for key in ("name", "original_filename", "audio_format"):
        if key not in project_row:
            raise InvalidArchiveError(f"{_MANIFEST_NAME} 'project' is missing {key!r}")
        if not isinstance(project_row[key], str):
            raise InvalidArchiveError(f"{_MANIFEST_NAME} 'project' {key!r} must be a string")

    for table, required_keys in (
        ("jobs", _JOB_REQUIRED_KEYS),
        ("agent_runs", _AGENT_RUN_REQUIRED_KEYS),
        ("revisions", _REVISION_REQUIRED_KEYS),
    ):
        rows = manifest.get(table, [])
        if not isinstance(rows, list):
            raise InvalidArchiveError(f"{_MANIFEST_NAME} {table!r} must be a list")
        for row in rows:
            if not isinstance(row, dict):
                raise InvalidArchiveError(f"{_MANIFEST_NAME} {table!r} entries must be objects")
            missing = [key for key in required_keys if key not in row]
            if missing:
                raise InvalidArchiveError(f"{_MANIFEST_NAME} {table!r} entry is missing {missing}")
            # レビュー指摘(MIDDLE): キーの存在だけでなく型も検証する。例えば
            # `score_snapshot`が文字列以外だと`_rewrite_project_id_in_score_snapshot`の
            # `json.loads`が`TypeError`を送出し、`InvalidArchiveError`(422)ではなく
            # 未処理例外(API層で500)になっていた。全ての必須キー
            # (`_JOB_REQUIRED_KEYS`/`_AGENT_RUN_REQUIRED_KEYS`/
            # `_REVISION_REQUIRED_KEYS`)は`db.py`の`TEXT NOT NULL`列に対応する
            # (`jobs.id/stage/status/created_at/updated_at`、
            # `agent_runs.id/status/created_at`、
            # `revisions.id/name/score_snapshot/created_at`はいずれもNOT NULL)。
            # `None`を許容すると、この検証をすり抜けてもINSERT時にNOT NULL
            # 制約違反で未処理例外(500)になるだけなので、一律で文字列を要求する
            # (レビュー指摘2巡目: これらの列がNULL許容だと往復を壊すのではとの
            # 懸念があったが、上記の通りスキーマ上NOT NULLのため該当しない)。
            not_strings = [key for key in required_keys if not isinstance(row[key], str)]
            if not_strings:
                raise InvalidArchiveError(
                    f"{_MANIFEST_NAME} {table!r} entry has non-string values for {not_strings}"
                )
            # レビュー指摘(2巡目、MIDDLE): 上のチェックは必須キーの文字列型しか
            # 見ておらず、任意キー(jobsのparams_json/exit_code/progress、
            # agent_runsのturns/usage_json/staged_ops_count/error、revisionsの
            # description/op_count)は未検証のままだった。これらはSQLiteの
            # 列へそのままバインドされるため、dict/listが紛れ込むと
            # `sqlite3.InterfaceError`(未処理例外、500)になる。SQLiteへ
            # バインド可能な値はJSONのスカラー(文字列/数値/真偽値/null)のみ
            # なので、行の全キーについて非スカラー(dict/list)だけを一律で
            # 拒否する(列ごとの型までは要求しない、他の型不一致はSQLite自体の
            # 型親和性で許容されるため実害が無い)。
            non_scalar = [key for key, value in row.items() if isinstance(value, dict | list)]
            if non_scalar:
                raise InvalidArchiveError(
                    f"{_MANIFEST_NAME} {table!r} entry has non-scalar values for {non_scalar}"
                )

        # レビュー指摘(3巡目、MIDDLE): `agent_runs`は`import_project_archive`が
        # `{old_id: new_id}`の辞書(`agent_run_id_map`)を作るため、`id`が
        # 重複した行があると後の行が前の行を上書きし、2行が同じ新規IDへ
        # 写像される。結果`INSERT INTO agent_runs`の2行目で主キー(`id`)の
        # UNIQUE制約違反(`sqlite3.IntegrityError`、未処理例外で500)になる。
        # jobs/revisionsは行ごとに独立して`ids.new_id()`を呼ぶため実害は
        # 無いが、3テーブルとも元は同一プロジェクトのSQLite主キー(重複
        # しないはず)なので、一律で重複IDを拒否しておく。
        seen_ids = [row["id"] for row in rows]
        duplicate_ids = sorted({rid for rid in seen_ids if seen_ids.count(rid) > 1})
        if duplicate_ids:
            raise InvalidArchiveError(
                f"{_MANIFEST_NAME} {table!r} has duplicate ids: {duplicate_ids}"
            )

    return manifest


def _check_archive_size(zf: zipfile.ZipFile) -> None:
    """展開前に、zipの中央ディレクトリだけから合計サイズ・件数を見積もって拒否する。"""
    total_bytes = 0
    member_count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        member_count += 1
        total_bytes += info.file_size
    if member_count > _MAX_ARCHIVE_MEMBERS:
        raise InvalidArchiveError(
            f"archive has too many entries ({member_count} > {_MAX_ARCHIVE_MEMBERS})"
        )
    if total_bytes > _MAX_UNCOMPRESSED_BYTES:
        raise InvalidArchiveError(
            f"archive's uncompressed size ({total_bytes} bytes) exceeds the limit "
            f"({_MAX_UNCOMPRESSED_BYTES} bytes)"
        )


def _remap_agent_run_path(rel_posix_path: str, agent_run_id_map: dict[str, str]) -> str:
    """agent run の`run_id`をパスに含む展開先を新IDへ付け替える。

    2箇所が対象(#65レビュー指摘、HIGH: 最初は`agent/`配下のディレクトリ名
    しか付け替えておらず、`score/staging/{run_id}.json`のファイル名が
    旧run_idのまま残っていた):
    - `agent/{run_id}/...`(`storage.agent_workspace_dir`)
    - `score/staging/{run_id}.json`(`storage.score_staging_path`、L2の
      提案がステージングされたScoreIR。L1整音のrun_idは`agent_runs`テーブルの
      行を持たないため`agent_run_id_map`に無く、その場合は元のファイル名を
      維持する)。
    """
    parts = rel_posix_path.split("/")
    if len(parts) >= 2 and parts[0] == "agent" and parts[1] in agent_run_id_map:
        parts[1] = agent_run_id_map[parts[1]]
        return "/".join(parts)
    if (
        len(parts) == 3
        and parts[0] == "score"
        and parts[1] == "staging"
        and parts[2].endswith(".json")
    ):
        old_run_id = parts[2][: -len(".json")]
        if old_run_id in agent_run_id_map:
            parts[2] = f"{agent_run_id_map[old_run_id]}.json"
            return "/".join(parts)
    return rel_posix_path


def _rewrite_project_id_in_file(path: Path, new_project_id: str) -> None:
    """ファイル内のScore IRが持つ`project_id`を新IDへ書き換える(#65レビュー指摘、HIGH)。

    `score/current.json`/`score/staging/{run_id}.json`はScoreIR全体のJSONで、
    自身の`project_id`フィールドを持つ(§10.2)。DB行のIDだけ新規採番して
    ファイル内容をそのままコピーすると、この`project_id`が旧プロジェクトを
    指したまま残り、L2エージェント向けコンテキスト(`agent/workspace.py`の
    `generate_context_markdown`)等に誤った情報が出続ける。読み込み不能な
    内容(壊れたJSON等)は`import_project_archive`のzip-slipチェックと同様に
    ここでは検証しない(展開そのものは既に完了しており、内容の妥当性は
    後続の`GET /score`等が`migrate_to_current`で検証する)。
    """
    try:
        data = storage.read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    if isinstance(data, dict) and "project_id" in data:
        data["project_id"] = new_project_id
        storage.write_json(path, data)


def _rewrite_project_id_in_score_snapshot(score_snapshot: str, new_project_id: str) -> str:
    """`revisions.score_snapshot`(ScoreIR全体のJSON文字列)内の`project_id`を書き換える。

    `_rewrite_project_id_in_file`と同じ理由(モジュール内docstring参照)。
    リビジョン復元(`RevisionService.restore_revision`)はこの文字列を
    そのまま`score/current.json`へ書き戻すため、ここで直しておかないと
    復元のたびに旧`project_id`が復活してしまう。
    """
    try:
        data = json.loads(score_snapshot)
    except json.JSONDecodeError:
        return score_snapshot
    if isinstance(data, dict) and "project_id" in data:
        data["project_id"] = new_project_id
        return json.dumps(data, ensure_ascii=False)
    return score_snapshot


def import_project_archive(
    workspace_dir: Path, service: ProjectService, archive_bytes: bytes
) -> dict:
    """アーカイブを新規プロジェクトとして読み込む。

    `project_id`を含む全てのDB行IDは新規採番する(モジュールdocstring参照)。
    zip-slip対策: 展開先パスは`.resolve()`後に必ず新規プロジェクトディレクトリの
    配下に収まることを確認してから書き込む。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise InvalidArchiveError("file is not a valid zip archive") from exc

    with zf:
        _check_archive_size(zf)
        try:
            manifest_raw = zf.read(_MANIFEST_NAME)
        except KeyError as exc:
            raise InvalidArchiveError(f"archive is missing {_MANIFEST_NAME}") from exc
        except (zipfile.BadZipFile, RuntimeError) as exc:
            # #65レビュー指摘(MIDDLE): zip自体は開けても、CRC不一致(壊れた
            # メンバー)は`BadZipFile`、暗号化されたメンバーは`RuntimeError`を
            # `zf.read`が送出する。422として扱う(そのままだとAPI層で
            # 未処理例外として500になってしまう)。
            raise InvalidArchiveError(f"could not read {_MANIFEST_NAME}: {exc}") from exc
        try:
            manifest = json.loads(manifest_raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidArchiveError(f"{_MANIFEST_NAME} is not valid JSON") from exc
        manifest = _validate_manifest(manifest)

        new_project_id = ids.new_id("proj")
        target_dir = storage.project_dir(workspace_dir, new_project_id)
        target_root = target_dir.resolve()

        agent_run_id_map = {run["id"]: ids.new_id("run") for run in manifest.get("agent_runs", [])}

        storage.ensure_project_layout(workspace_dir, new_project_id)
        try:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith(_MEMBER_PREFIX):
                    continue
                rel = info.filename[len(_MEMBER_PREFIX) :]
                if not rel:
                    continue
                rel = _remap_agent_run_path(rel, agent_run_id_map)
                dest = (target_dir / rel).resolve()
                if not dest.is_relative_to(target_root):
                    raise InvalidArchiveError(
                        f"archive member escapes the project directory: {info.filename!r}"
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with zf.open(info) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                except (zipfile.BadZipFile, RuntimeError) as exc:
                    # #65レビュー指摘(MIDDLE): CRC不一致・暗号化されたメンバー
                    # を422として扱う(manifest読み込みと同じ理由)。
                    raise InvalidArchiveError(
                        f"could not extract archive member {info.filename!r}: {exc}"
                    ) from exc

            # #65レビュー指摘(HIGH): 展開したファイル自体が持つ`project_id`
            # (`score/current.json`/`score/staging/*.json`)も新IDへ書き換える。
            _rewrite_project_id_in_file(
                storage.score_current_path(workspace_dir, new_project_id), new_project_id
            )
            staging_dir = target_dir / "score" / "staging"
            for staging_path in sorted(staging_dir.glob("*.json")):
                _rewrite_project_id_in_file(staging_path, new_project_id)

            created_at = datetime.now(UTC).isoformat()
            conn = db.get_connection(workspace_dir / "db.sqlite3")
            project_row = manifest["project"]
            conn.execute(
                "INSERT INTO projects(id, name, original_filename, audio_format, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    new_project_id,
                    project_row["name"],
                    project_row["original_filename"],
                    project_row["audio_format"],
                    created_at,
                ),
            )
            for job in manifest.get("jobs", []):
                conn.execute(
                    "INSERT INTO jobs(id, project_id, stage, status, progress, message, "
                    "params_json, exit_code, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ids.new_id("job"),
                        new_project_id,
                        job["stage"],
                        job["status"],
                        job.get("progress", 0.0),
                        job.get("message"),
                        job.get("params_json"),
                        job.get("exit_code"),
                        job["created_at"],
                        job["updated_at"],
                    ),
                )
            for run in manifest.get("agent_runs", []):
                conn.execute(
                    "INSERT INTO agent_runs(id, project_id, status, turns, usage_json, "
                    "staged_ops_count, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        agent_run_id_map[run["id"]],
                        new_project_id,
                        run["status"],
                        run.get("turns", 0),
                        run.get("usage_json"),
                        run.get("staged_ops_count", 0),
                        run.get("error"),
                        run["created_at"],
                    ),
                )
            for rev in manifest.get("revisions", []):
                conn.execute(
                    "INSERT INTO revisions(id, project_id, name, description, op_count, "
                    "score_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        ids.new_id("rev"),
                        new_project_id,
                        rev["name"],
                        rev.get("description"),
                        rev.get("op_count", 0),
                        _rewrite_project_id_in_score_snapshot(
                            rev["score_snapshot"], new_project_id
                        ),
                        rev["created_at"],
                    ),
                )
            conn.commit()
        except BaseException:
            conn = db.get_connection(workspace_dir / "db.sqlite3")
            conn.rollback()
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    return service.get_project(new_project_id)
