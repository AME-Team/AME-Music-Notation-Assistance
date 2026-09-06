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
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import soundfile as sf

from app.config import resolve_workspace_dir
from app.domain.score import Clef, Note, Part, Pedal, ScoreIR, SourceInfo
from app.infra import storage
from app.pipeline.beat import run_beat_estimation
from app.pipeline.separate import audio_fingerprint, params_hash, resolve_model, run_separation
from app.pipeline.transcribe.piano import run_piano_transcription
from app.services.score_service import ScoreService


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


def _separate_artifacts_exist(workspace_dir: Path, project_id: str) -> Callable[[dict], bool]:
    """separateステージの `should_skip_stage(artifacts_exist=...)` コールバックを組み立てる。

    (#21-M1レビュー指摘の追加ラウンド)
    - ハッシュが一致しても、メタデータに記録された期待ステム名が現在ディスク上に
      ある集合に全て含まれていなければスキップしない。ジョブが強制終了され
      `except BaseException` クリーンアップ(メタデータ削除)が走らなかった場合や、
      ステムを(一部だけでも)手動削除した場合に、旧メタデータだけが残って欠落した
      成果物をそのまま使い回してしまうのを防ぐ。「1つでも存在すればOK」という緩い
      判定だと部分削除を見逃すため部分集合判定にする。完全一致(`==`)ではなく部分
      集合にするのは、モデル切替の残骸等で無関係な余分な .wav がディレクトリに
      残っていても、期待した成果物自体は全て揃っていればそれでよく、余分ファイルの
      存在だけでスキップ最適化(§6)が永久に無効化されるのは望ましくないため。
    - `meta` に `artifact_names` キーが無い場合(この機能を追加する前に書かれた
      旧メタデータ)は fail-closed でスキップしない。`set() <= 任意の集合` は
      空集合の部分集合判定として常にTrueになる(vacuous truth)ため、キー欠如を
      「チェック不要」と誤解釈するとこの機能追加自体が無意味になってしまう。
      再実行すれば新形式のメタデータで自己修復するため、一度だけ余分な実行が
      発生するに留まる。
    - `set(...).issubset(...)` を使う(`<=` ではなく): `list_stem_names()` は
      実際には `set[str]` を返すため `<=` でも動作するが、`issubset()` は
      任意のiterableを受け付けるためより頑健で、将来 `list_stem_names()` の
      戻り値型が変わっても壊れにくい。
    """

    def _check(meta: dict) -> bool:
        if "artifact_names" not in meta:
            return False
        return set(meta["artifact_names"]).issubset(
            storage.list_stem_names(workspace_dir, project_id)
        )

    return _check


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
    if storage.should_skip_stage(
        workspace_dir,
        project_id,
        "separate",
        hash_value,
        artifacts_exist=_separate_artifacts_exist(workspace_dir, project_id),
    ):
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
        # 次回実行時のスキップ判定で、期待するステムが全てディスク上に揃っているか
        # (部分集合として)確認するために記録する(#21-M1レビュー指摘の追加ラウンド)。
        # `written` のキーは `run_separation` の型注釈上 `dict[str, Path]` で、
        # 常に拡張子無しの裸のステム名(`storage.list_stem_names()` と同じ形式)
        # であることが前提。`str()` はこの前提の上で型チェッカーに意図を明示する
        # ためのもので、キーの実体が変わった場合(例えばフルパスの `Path` に
        # なった場合)まで自動的に吸収するものではない -- その場合は `str()` を
        # 通しても `list_stem_names()` の拡張子無しステム名とは形式が食い違い、
        # 比較は依然として常にFalseになる。その変更をするなら、この関数側の
        # 正規化ではなく `run_separation` の戻り値契約自体を見直すこと。
        artifact_names=sorted(str(name) for name in written),
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
    if storage.should_skip_stage(
        workspace_dir,
        project_id,
        "beat",
        hash_value,
        # separateステージと同じ理由(#21-M1レビュー指摘の追加ラウンド): beatmap.json
        # を手動削除してもメタデータだけ残っていれば、GET /analysis/beatmap が
        # 404を返し続けるのに再実行では復旧しない、という事故を防ぐ。
        artifacts_exist=lambda _meta: storage.beatmap_path(workspace_dir, project_id).exists(),
    ):
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


PIANO_STEM_NAME = "piano"


def _initial_score_ir(project_id: str, workspace_dir: Path) -> ScoreIR:
    """Score IR 未作成時(#23)の初期化。

    source情報は原曲ファイルから直接読む(DSP WorkerはDBを見ずファイルだけで
    完結する設計、#16/#18の `find_original_audio` と同じ方針)。`tempo_map`/
    `time_signatures` はまだ空のままにする: これらはStage 4(#25)が
    `beatmap.json` から取り込む(transcribeステージ自体はbeat推定の結果に
    依存しないため、依存グラフ上も `beat -> quantize` のみで `beat ->
    transcribe` は無い)。
    """
    audio_path = storage.find_original_audio(workspace_dir, project_id)
    info = sf.info(str(audio_path))
    return ScoreIR(
        project_id=project_id,
        source=SourceInfo(
            filename=audio_path.name,
            duration_sec=float(info.duration),
            sample_rate=int(info.samplerate),
        ),
    )


def _piano_part_has_notes(workspace_dir: Path, project_id: str) -> bool:
    """`should_skip_stage` の `artifacts_exist` 用(#24-M2レビュー指摘の想定):

    Score IR自体、または `piano` パートのノートが(手動削除等で)無くなっていれば
    スキップを拒否する。M1の分離/ビート推定ステージと同じ保護パターン。
    """
    score = ScoreService(workspace_dir=workspace_dir).read_score_optional(project_id)
    if score is None:
        return False
    part = score.find_part(PIANO_STEM_NAME)
    return part is not None and len(part.notes) > 0


def run_transcribe_stage(job_id: str, project_id: str, workspace_dir: Path, params: dict) -> None:
    """#24: Stage 3 ピアノAMT。

    §6: 入力(pianoステム)+使用ライブラリのバージョンが不変ならスキップする
    (separate/beatステージと対称)。**この段階では一切ノートを捨てない**:
    `run_piano_transcription` が返す `ghost_candidate` フラグはそのまま
    `Note.flags` へ引き継ぎ、削除はしない。
    """
    emit(
        {
            "job_id": job_id,
            "stage": "transcribe",
            "progress": 0.0,
            "message": "transcribing piano",
        }
    )

    piano_stem_path = storage.stems_dir(workspace_dir, project_id) / f"{PIANO_STEM_NAME}.wav"
    if not piano_stem_path.exists():
        # #16: pianoステムは `standard` プリセット(htdemucs_6s)でのみ生成される。
        # `fast`/`high_quality`(4ステム)には無い(M2時点の既知の制約)。
        raise ValueError(
            "piano stem not found; run the separate stage with the 'standard' preset "
            "first (only htdemucs_6s produces a dedicated piano stem)"
        )

    piano_transcription_inference_version = _package_version("piano_transcription_inference")
    hash_payload = json.dumps(
        {
            "audio_fingerprint": audio_fingerprint(piano_stem_path),
            "package_version": piano_transcription_inference_version,
        },
        sort_keys=True,
    )
    hash_value = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    if storage.should_skip_stage(
        workspace_dir,
        project_id,
        "transcribe",
        hash_value,
        artifacts_exist=lambda _meta: _piano_part_has_notes(workspace_dir, project_id),
    ):
        emit(
            {
                "job_id": job_id,
                "stage": "transcribe",
                "progress": 1.0,
                "message": "skipped (unchanged input)",
            }
        )
        return

    score_service = ScoreService(workspace_dir=workspace_dir)
    score_path = storage.score_current_path(workspace_dir, project_id)
    # 楽観的並行性制御(#20/beatステージと同じ思想、#29の考え方の先取り): 推論には
    # 数十秒かかるため、その間に他プロセス(将来のM3編集APIや並行ジョブ)がScore IR
    # を書き換えている可能性がある。**推論を開始する前**に読んだ生JSONと、書き込み
    # 直前に再読込した生JSONを比較し、食い違っていれば上書きしない。この読み取りは
    # 必ず `run_piano_transcription` の呼び出しより前に行うこと(#24-M2レビュー
    # ラウンドで、推論後に読んでしまいレース窓を検出できていなかった実装ミスを修正)。
    # M2時点ではScore IRを書き換える経路がこのステージ自身以外に無いため実際には
    # 発火しないが、M3で編集APIが入った際にも安全側に倒れる設計として先に
    # 用意しておく。
    raw_before = storage.read_json(score_path) if score_path.exists() else None

    result = run_piano_transcription(piano_stem_path)

    score = score_service.read_score_optional(project_id) or _initial_score_ir(
        project_id, workspace_dir
    )
    part = score.find_part(PIANO_STEM_NAME)
    if part is None:
        part = Part(
            id=PIANO_STEM_NAME,
            name="Piano",
            midi_program=0,
            stem_source=f"stems/{PIANO_STEM_NAME}.wav",
            staves=2,
            clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
        )
        score.parts.append(part)

    # #29の考え方を先取り: このステージが書き込むのは provenance="amt" のノートのみ。
    # 将来M3で手動編集(provenance="user")が入っても、再採譜がそれを消さない。
    preserved_notes = [n for n in part.notes if n.provenance != "amt"]
    new_notes = [
        Note(
            id=score.allocate_note_id(),
            onset_sec=event.onset_sec,
            duration_sec=event.duration_sec,
            midi=event.midi,
            velocity=event.velocity,
            provenance="amt",
            flags=["ghost_candidate"] if event.ghost_candidate else [],
        )
        for event in result.notes
    ]
    part.notes = preserved_notes + new_notes
    part.pedals = [Pedal(start_sec=p.start_sec, stop_sec=p.stop_sec) for p in result.pedals]

    raw_now = storage.read_json(score_path) if score_path.exists() else None
    if raw_now != raw_before:
        emit(
            {
                "job_id": job_id,
                "stage": "transcribe",
                "progress": 1.0,
                "message": "skipped write: score/current.json was modified concurrently; "
                "not overwriting",
            }
        )
        return

    score_service.write_score(project_id, score)
    storage.write_stage_metadata(
        workspace_dir,
        project_id,
        "transcribe",
        params_hash=hash_value,
        provider_versions={"piano_transcription_inference": piano_transcription_inference_version},
    )
    emit(
        {
            "job_id": job_id,
            "stage": "transcribe",
            "progress": 1.0,
            "message": f"{len(new_notes)} notes, {len(part.pedals)} pedal events",
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
        elif stage == "transcribe":
            run_transcribe_stage(job_id, project_id, workspace_dir, params)
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
