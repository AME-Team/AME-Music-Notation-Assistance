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
from collections.abc import Callable
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


def score_current_path(workspace_dir: Path, project_id: str) -> Path:
    """Score IR(#23, 設計書§10.3)の永続化先。"""
    return project_dir(workspace_dir, project_id) / "score" / "current.json"


def score_ops_log_path(workspace_dir: Path, project_id: str) -> Path:
    """#32: 編集オペレーションの追記専用監査ログ(設計書§10.4)。

    `score/staging`/`score/revisions`(`ensure_project_layout`参照)はL1/L2 AI整音の
    提案差分用に予約されたディレクトリのため、Undo/Redo用のファイルはそれらとは
    別に`score/`直下へ置く。
    """
    return project_dir(workspace_dir, project_id) / "score" / "ops.jsonl"


def score_undo_state_path(workspace_dir: Path, project_id: str) -> Path:
    """#32: Undo/Redoの2本のスタック(`done`/`undone`)の永続化先。"""
    return project_dir(workspace_dir, project_id) / "score" / "undo_state.json"


def musicxml_export_path(workspace_dir: Path, project_id: str) -> Path:
    """#27 Stage 6: `POST /export` が書き出すMusicXMLの保存先。"""
    return project_dir(workspace_dir, project_id) / "export" / "score.musicxml"


def midi_export_path(workspace_dir: Path, project_id: str) -> Path:
    """#27 FR-13: Standard MIDI File のエクスポート先。"""
    return project_dir(workspace_dir, project_id) / "export" / "score.mid"


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
    artifact_names: list[str] | None = None,
) -> None:
    """NFR-11: 実行パラメータ・使用モデル・プロバイダのバージョンを成果物メタデータに記録する。

    §6: パラメータのハッシュも記録し、入力・パラメータ不変ならスキップできるようにする。

    `artifact_names`(省略可)は、このステージが実際に書き出した成果物名の一覧
    (例: separateステージのステム名)。`should_skip_stage` の `artifacts_exist`
    コールバックが、記録した成果物名がディスク上の現在の成果物集合に全て含まれる
    か(部分集合として)確認できるようにする(#21-M1レビュー指摘の追加ラウンド:
    一部だけ手動削除された場合、単に「1つでも存在すればOK」という判定では
    見逃してしまうため。完全一致ではなく部分集合にするのは、モデル切替の残骸等
    の無関係な余分ファイルが残っていてもスキップ最適化を永久に無効化しないため)。
    """
    meta: dict = {"stage": stage, "params_hash": params_hash, "versions": provider_versions}
    if artifact_names is not None:
        meta["artifact_names"] = artifact_names
    write_json(stage_metadata_path(workspace_dir, project_id, stage), meta)


def should_skip_stage(
    workspace_dir: Path,
    project_id: str,
    stage: str,
    params_hash: str,
    *,
    artifacts_exist: Callable[[dict], bool] | None = None,
) -> bool:
    """§6: 直前の実行と同じパラメータハッシュなら再実行をスキップしてよいと判定する。

    `artifacts_exist`(省略可)は、ハッシュが一致した場合に実際の成果物(ステム
    ファイル/beatmap.json等)が存在するかも確認するコールバック(#21-M1レビュー
    指摘の追加ラウンド)。ハッシュだけで判定すると、(1) ステム/beatmap.jsonを
    (一部だけでも)手動削除してもメタデータだけ残っていれば配信APIが404を
    返し続ける、(2) 分離実行中にジョブが強制終了され `run_separate_stage` の
    `except BaseException` クリーンアップ(メタデータ削除)が走らなかった場合に
    旧プリセットのメタデータが残ったまま残留する、という2つのシナリオでスキップ
    が誤判定される。

    コールバックには読み込み済みの `meta` 辞書を渡す(#21-M1レビュー指摘の
    追加ラウンド)。呼び出し元が `write_stage_metadata` に記録した期待成果物
    名一覧を `meta` から読み、現在のディスク状態と突き合わせられるようにする。

    既知の限界(#21-M1レビュー指摘の追加ラウンド): この仕組みはファイル名の
    存在/欠落しか検証しない。`_write_wav_atomically`/`write_json` はいずれも
    一時ファイル→`os.replace` で単一ファイル単位のアトミック性は保証するため、
    ある1つのステムファイルが中途半端な内容のまま残ることは無いが、「複数の
    ステムファイルにまたがる更新」自体はアトミックではない。そのため、
    強制終了のタイミング次第では、同名のステムファイルが(直前の別プリセット
    実行によって)*完全に書き終わった別内容*で存在し、名前だけを見る限り
    「揃っている」ように見えてしまうケースまでは検出できない。この限界を
    完全に塞ぐには、成果物ディレクトリ全体をステージング→アトミックに入れ替える
    設計への変更が必要(README「既知の制約」の分離失敗時の一貫性の記述と同種)。
    """
    meta_path = stage_metadata_path(workspace_dir, project_id, stage)
    if not meta_path.exists():
        return False
    try:
        meta = read_json(meta_path)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(meta, dict):
        # `read_json` はJSONとして有効なら何でも返す(例: メタデータファイルの
        # 内容が `null` や配列であっても成功する)。dict以外だと直後の
        # `meta.get(...)` がAttributeErrorを送出し、この関数のfail-safe方針
        # (メタデータ読み取り不能なら再実行させる)に反してworkerのmainまで
        # 未処理例外が伝播してしまう(#21-M1レビュー指摘の追加ラウンド)。
        return False
    if meta.get("params_hash") != params_hash:
        return False
    if artifacts_exist is None:
        return True
    try:
        # この関数はメタデータ読み取り不能なら再実行させるfail-safe設計。
        # `artifacts_exist` はディスクI/O(例: `list_stem_names` の `glob`)を
        # 行うため、権限エラー等の一時的なOSErrorで無防備に例外を送出すると、
        # 「スキップ判定できない→再実行」ではなく「ジョブ全体が未処理例外で
        # クラッシュする」という、この関数のfail-safe方針と矛盾する経路に
        # なってしまう(#21-M1レビュー指摘の追加ラウンド)。TypeErrorも含めるのは、
        # `meta["artifact_names"]` がJSONとしては有効でも非イテラブルな不正値
        # (例: 数値)だった場合、呼び出し元が `set(...)` に通した際に送出しうる
        # ため(壊れたメタデータもまた「判定できない」の一種として扱う)。
        return artifacts_exist(meta)
    except (OSError, TypeError):
        return False


def delete_project_dir(workspace_dir: Path, project_id: str) -> None:
    import shutil

    base = project_dir(workspace_dir, project_id)
    if base.exists():
        shutil.rmtree(base)
