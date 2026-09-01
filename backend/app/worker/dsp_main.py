"""DSP Worker エントリ(#13, #16, #18)。

API サーバとは別プロセスで起動される(NFR-04)。進捗は stdout に JSON Lines で
1行ずつ出力し、親プロセス(job_service)がそれを読み取って SSE に流す。

使い方:
    python -m app.worker.dsp_main <job_id> <project_id> <stage> <params_json>

`stage == "dummy"` の params_json に `{"crash_at": 0.4}` を含めると、指定した
進捗到達時に例外を投げて異常終了する(#13完了条件の検証用)。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app.config import resolve_workspace_dir
from app.infra import storage
from app.pipeline.beat import run_beat_estimation
from app.pipeline.separate import audio_fingerprint, params_hash, resolve_model, run_separation


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _package_version(name: str) -> str:
    """NFR-11: 成果物メタデータに記録するプロバイダのバージョン。"""
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def run_dummy_stage(job_id: str, params: dict) -> None:
    crash_at = params.get("crash_at")
    steps = 5
    for i in range(steps + 1):
        progress = i / steps
        if crash_at is not None and progress >= crash_at:
            emit(
                {
                    "job_id": job_id,
                    "stage": "dummy",
                    "progress": progress,
                    "message": "simulated crash",
                }
            )
            raise RuntimeError("simulated worker crash (crash_at reached)")
        emit(
            {
                "job_id": job_id,
                "stage": "dummy",
                "progress": progress,
                "message": f"dummy stage: {int(progress * 100)}%",
            }
        )
        time.sleep(0.05)


def run_separate_stage(job_id: str, project_id: str, workspace_dir: Path, params: dict) -> None:
    """#16: Stage 1 音源分離。§6: パラメータ不変・入力不変ならスキップする。"""
    preset = params.get("preset", "standard")
    execution_provider = params.get("execution_provider", "auto")

    emit({"job_id": job_id, "stage": "separate", "progress": 0.0, "message": f"preset={preset}"})

    audio_path = storage.find_original_audio(workspace_dir, project_id)
    resolved_model = resolve_model(preset)
    demucs_onnx_version = _package_version("demucs-onnx")
    # スキップ判定は解決済みモデルIDでハッシュ化する(プリセットラベルではない)。
    # プリセット→モデルの対応表を将来変更しても、プリセット名だけをハッシュに
    # 使っていると異なるモデルで生成した古いステムを誤って使い回してしまう
    # (NFR-11のトレーサビリティ方針と矛盾する)。demucs-onnxのバージョンも同様の
    # 理由で含める(beatステージがbeat-thisのバージョンを含めるのと対称)。
    hash_value = params_hash(
        model=resolved_model,
        execution_provider=execution_provider,
        audio_fingerprint_value=audio_fingerprint(audio_path),
        package_version=demucs_onnx_version,
    )
    if storage.should_skip_stage(workspace_dir, project_id, "separate", hash_value):
        emit(
            {
                "job_id": job_id,
                "stage": "separate",
                "progress": 1.0,
                "message": "skipped (unchanged params/input)",
            }
        )
        return

    output_dir = storage.stems_dir(workspace_dir, project_id)
    previous_stem_names = storage.list_stem_names(workspace_dir, project_id)

    try:
        written = run_separation(
            audio_path, output_dir, preset=preset, execution_provider=execution_provider
        )
    except BaseException:
        # 分離が途中で失敗すると、ループの一部(新ステム)だけ書き込まれ残りは
        # 旧ステムのままという新旧混在状態になりうる(#16レビュー指摘)。
        # ファイル自体を揃える完全な原子性(全ステムをステージング先に書いてから
        # ディレクトリごと入れ替える等)はここでは実装していない既知の制約だが、
        # 少なくとも新旧どちらのステム名のピークキャッシュも無効化しておくことで、
        # 矛盾した(古い)波形データがUIに残り続けることは避ける。
        current_stem_names = storage.list_stem_names(workspace_dir, project_id)
        storage.invalidate_peaks_cache(
            workspace_dir, project_id, list(previous_stem_names | current_stem_names)
        )
        # stage_metadata(params_hash)も削除する(#20-M1レビュー指摘の追加ラウンド)。
        # 残したままだと、「standardで成功→fastへの切替が途中失敗(混在ステムが残る)
        # →standardを再実行」という流れで should_skip_stage が前回成功時の standard
        # ハッシュと一致してしまい、混在した破損ステム群を検証せずスキップしてしまう。
        # 失敗直後はメタデータ自体を「不明な状態」として扱い、次回は必ず実行させる。
        storage.stage_metadata_path(workspace_dir, project_id, "separate").unlink(missing_ok=True)
        raise

    # プリセットを変えて再分離すると、新プリセットには含まれないステムが残り続ける
    # (例: standard[6stem] → fast[4stem] で guitar/piano が孤児化する)。
    # ファイルとピークキャッシュの両方から一掃する。
    stale_stem_names = previous_stem_names - set(written)
    for name in stale_stem_names:
        (output_dir / f"{name}.wav").unlink(missing_ok=True)
    # 再分離でステムを上書きした場合、古い波形ピークキャッシュを消す(#21)。
    # 消さないと get_peaks() がキャッシュを再利用し続け、プリセット/EPを変えて
    # 再分離しても古いステムの波形が表示され続けてしまう。
    # "original"(原曲)のキャッシュは対象外: separateステージはステムだけを
    # 書き換え、原曲ファイル自体には触れないため、原曲の波形は不変(#20-M1
    # レビュー指摘の追加ラウンド)。原曲差し替え経路(現状未実装)が将来入る際は
    # そちらの実装側で"original"キャッシュの無効化を担う必要がある。
    storage.invalidate_peaks_cache(workspace_dir, project_id, list(set(written) | stale_stem_names))
    storage.write_stage_metadata(
        workspace_dir,
        project_id,
        "separate",
        params_hash=hash_value,
        provider_versions={
            "demucs_onnx": demucs_onnx_version,
            # NFR-11: 実際に推論で使われたモデル識別子(例: "htdemucs_6s")を記録する。
            # プリセット名("standard"等)はUI都合のラベルでしかなく、将来プリセット→
            # モデルの対応表を変えると成果物のトレーサビリティが失われる。
            "model": resolved_model,
            "preset": preset,
        },
    )
    emit(
        {
            "job_id": job_id,
            "stage": "separate",
            "progress": 1.0,
            "message": f"wrote {len(written)} stems",
        }
    )


def run_beat_stage(job_id: str, project_id: str, workspace_dir: Path) -> None:
    """#18/#19: Stage 2 ビート・ダウンビート・拍子推定。

    §6: 入力(原曲)+使用チェックポイントのバージョンが不変ならスキップする
    (separateステージがmodel/execution_providerをハッシュに含めるのと対称)。
    ハッシュに beat-this のバージョンも含めないと、パッケージを更新しても古い
    beatmap.json がスキップにより残り続け、NFR-11のトレーサビリティと矛盾する。
    これにより、BeatGridEditor(#20)での手動補正後に誤って再実行して上書きしてしまう
    事故も防げる。強制再実行(force)のAPIは未実装(separateステージも同様の制約)。
    """
    emit({"job_id": job_id, "stage": "beat", "progress": 0.0, "message": "estimating beats"})

    audio_path = storage.find_original_audio(workspace_dir, project_id)
    beat_this_version = _package_version("beat-this")
    # separate側のparams_hash()と対称に、sha256(sort_keys付きJSON)で正規化する
    # (#20-M1レビュー指摘の追加ラウンド: 以前は平文結合で形式が揃っていなかった)。
    hash_payload = json.dumps(
        {
            "audio_fingerprint": audio_fingerprint(audio_path),
            "beat_this_version": beat_this_version,
            "checkpoint": "final0",
        },
        sort_keys=True,
    )
    hash_value = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
    if storage.should_skip_stage(workspace_dir, project_id, "beat", hash_value):
        emit(
            {
                "job_id": job_id,
                "stage": "beat",
                "progress": 1.0,
                "message": "skipped (unchanged input; manual edits preserved)",
            }
        )
        return

    beatmap_file = storage.beatmap_path(workspace_dir, project_id)
    result = run_beat_estimation(str(audio_path))

    # 楽観的並行性制御(#20レビュー指摘のlost-update対策): 推論には数秒〜数十秒
    # かかるため、その間にBeatGridEditorがPATCHでbeatmap.jsonを書き換えている
    # 可能性がある。書き込み直前に現在の `source` を確認し、既に "manual" なら
    # (=推論中に手動補正が入った)上書きしない。mtime比較はファイルシステムの
    # 時刻解像度(粗いと同一tick内の変更を検出できない)に依存するため使わず、
    # 内容(source)を直接見る。書き込み直前まで再読込を遅らせることで、
    # レース窓は「推論全体」から「チェック〜書き込みの一瞬」まで縮小される
    # (完全な排他制御ではないが、実用上十分な緩和策)。
    if beatmap_file.exists() and storage.read_json(beatmap_file).get("source") == "manual":
        # beatmap.json自体への書き込みはスキップするが、stage_metadataは今回の
        # (新しい音源に対する)ハッシュで更新しておく(#20-M1レビュー指摘の
        # 追加ラウンド)。更新しないと、should_skip_stageが古い音源のハッシュ
        # のままになり、同じ(新しい)音源でジョブを再実行するたびに毎回フル
        # 推論が走り、そのたびに書き込みだけがスキップされ続けてしまう。
        storage.write_stage_metadata(
            workspace_dir,
            project_id,
            "beat",
            params_hash=hash_value,
            provider_versions={"beat_this": beat_this_version, "checkpoint": "final0"},
        )
        emit(
            {
                "job_id": job_id,
                "stage": "beat",
                "progress": 1.0,
                "message": "skipped write: beatmap.json was manually edited during "
                "estimation; not overwriting",
            }
        )
        return

    storage.write_json(beatmap_file, {**result.to_dict(), "source": "auto"})
    storage.write_stage_metadata(
        workspace_dir,
        project_id,
        "beat",
        params_hash=hash_value,
        provider_versions={"beat_this": beat_this_version, "checkpoint": "final0"},
    )

    emit(
        {
            "job_id": job_id,
            "stage": "beat",
            "progress": 1.0,
            "message": f"{len(result.beats)} beats, {len(result.downbeats_sec)} downbeats",
        }
    )


def main() -> int:
    # #79: Windows コンソールの既定コードページに関わらず UTF-8 で出力する。
    # sys.stdout/stderr は typeshed 上 TextIO 型で reconfigure() を持たないため、
    # 実体である TextIOWrapper の場合のみ呼び出す(pytest の capsys 等、
    # TextIOWrapper でないストリームに差し替えられていても worker を落とさない)。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8")

    job_id, project_id, stage, params_json = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    params = json.loads(params_json) if params_json else {}
    workspace_dir = resolve_workspace_dir()

    try:
        if stage == "dummy":
            run_dummy_stage(job_id, params)
        elif stage == "separate":
            run_separate_stage(job_id, project_id, workspace_dir, params)
        elif stage == "beat":
            run_beat_stage(job_id, project_id, workspace_dir)
        else:
            emit(
                {
                    "job_id": job_id,
                    "stage": stage,
                    "progress": 0.0,
                    "message": f"unknown stage: {stage}",
                }
            )
            return 1
    except Exception as exc:  # noqa: BLE001 — ワーカーは失敗を exit code で伝える設計
        print(f"worker error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
