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
from typing import Any

import soundfile as sf

from app.config import resolve_workspace_dir
from app.domain.migrations import migrate_to_current
from app.domain.score import Clef, Note, Part, Pedal, ScoreIR, SnapCandidate, SourceInfo, Spelling
from app.infra import storage
from app.pipeline.beat import run_beat_estimation
from app.pipeline.quantize import DEFAULT_TOP_N, quantize_note_onsets, quantize_pedal_ticks
from app.pipeline.refine.baseline import RefineNoteInput, refine_baseline
from app.pipeline.separate import audio_fingerprint, params_hash, resolve_model, run_separation
from app.pipeline.transcribe.bass import BASS_ALGO_VERSION, run_bass_transcription
from app.pipeline.transcribe.guitar import (
    GUITAR_ALGO_VERSION,
    OTHER_ALGO_VERSION,
    run_guitar_transcription,
)
from app.pipeline.transcribe.piano import run_piano_transcription
from app.pipeline.transcribe.vocals import VOCALS_ALGO_VERSION, run_vocals_transcription
from app.services import score_undo, stage_invalidation
from app.services.score_service import ScoreService


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _invalidate_downstream_or_reset(workspace_dir: Path, project_id: str, stage: str) -> None:
    """`stage_invalidation.invalidate_downstream`を呼び、失敗時は自ステージの

    meta.jsonを削除してから例外を再送出する(#29-M2レビュー指摘)。

    通常(force無し)は入力/パラメータが変われば`should_skip_stage`がFalseに
    なるため、無効化に失敗しても次回実行時に自然にリトライされる。しかし
    `force=True`で入力が不変のまま再実行した場合、無効化がリトライ上限超過
    等で失敗しても自ステージのmeta.jsonが(不変入力と一致する)古いハッシュ
    のまま残っていると、次回の非force実行は`should_skip_stage`でスキップ
    されてしまい、下流の無効化が二度と再試行されない(成果物は実際に
    再生成されたのに下流がstaleにならないまま恒久的に残る)。自ステージの
    meta.jsonをここで削除しておけば、force指定の有無に関わらず次回実行時に
    必ず再実行され、無効化も再試行される。
    """
    try:
        stage_invalidation.invalidate_downstream(workspace_dir, project_id, stage)
    except BaseException:
        storage.stage_metadata_path(workspace_dir, project_id, stage).unlink(missing_ok=True)
        raise


def _reset_undo_history_or_warn(workspace_dir: Path, project_id: str) -> None:
    """#32-M3レビュー指摘: transcribe/quantize再実行後にUndo/Redoスタックをクリアする。

    `score_undo.reset_undo_state`のdocstring参照: transcribeはamtノートを新しい
    IDで作り直し、quantizeは全ノートのtick位置を再計算するため、再実行前の
    UndoEntryをそのまま適用すると消えたノートの復活や巻き戻りを起こしうる。
    Undo履歴はスコア本体より優先度の低い副次的な状態のため、リセット自体が
    (ディスクI/Oエラー等で)失敗してもステージを失敗させず警告に留める。
    """
    try:
        score_undo.reset_undo_state(storage.score_undo_state_path(workspace_dir, project_id))
    except OSError as exc:
        print(
            f"[dsp_main] warning: failed to reset undo history for project {project_id}: {exc}",
            file=sys.stderr,
        )


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
    force = params.get("force", False)

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
    if not force and storage.should_skip_stage(
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
    # #29: 自ステージのstage_metadataを書く前に下流(transcribe/quantize)を無効化する
    # (呼び出し契約は`services/stage_invalidation.py`のモジュールdocstring参照)。
    _invalidate_downstream_or_reset(workspace_dir, project_id, "separate")
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


def run_beat_stage(job_id: str, project_id: str, workspace_dir: Path, params: dict) -> None:
    """#18/#19/#29: Stage 2 ビート・ダウンビート・拍子推定。

    §6: 入力(原曲)+使用チェックポイントのバージョンが不変ならスキップする
    (separateステージがmodel/execution_providerをハッシュに含めるのと対称)。
    ハッシュに beat-this のバージョンも含めないと、パッケージを更新しても古い
    beatmap.json がスキップにより残り続け、NFR-11のトレーサビリティと矛盾する。
    これにより、BeatGridEditor(#20)での手動補正後に誤って再実行して上書きしてしまう
    事故も防げる。`params["force"]` でこのスキップ判定を明示的にバイパスできる(#29)。
    """
    force = params.get("force", False)
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
    if not force and storage.should_skip_stage(
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
    # #29: beatmap.jsonへ実際に新しい推論結果を書いた場合のみ下流(quantize)を
    # 無効化する(直前の「manual編集を優先しwrite skip」分岐はbeatmap.jsonの内容が
    # 変わっていないため対象外)。自ステージのstage_metadataを書く前に呼ぶこと
    # (呼び出し契約は`services/stage_invalidation.py`のモジュールdocstring参照)。
    _invalidate_downstream_or_reset(workspace_dir, project_id, "beat")
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
BASS_STEM_NAME = "bass"
VOCALS_STEM_NAME = "vocals"
GUITAR_STEM_NAME = "guitar"
OTHER_STEM_NAME = "other"


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


def _transcribe_artifacts_exist(
    workspace_dir: Path,
    project_id: str,
    *,
    require_piano: bool = False,
    require_bass: bool = False,
    require_vocals: bool = False,
    require_guitar: bool = False,
    require_other: bool = False,
    meta: dict[str, Any] | None = None,
) -> bool:
    """`should_skip_stage` の `artifacts_exist` 用(#24/#54/#55, #124/#125 レビュー指摘):

    Score IR自体、または存在する各ステムに対応するパートのノートが(手動削除等で)
    無くなっていればスキップを拒否する(AND条件)。
    ただし、メタデータに記録された採譜当時のノート数が 0 のステム(無音ステム)は、
    0 件のままで正常スキップを許可する(#125 レビュー指摘)。
    """
    score = ScoreService(workspace_dir=workspace_dir).read_score_optional(project_id)
    if score is None:
        return False

    recorded_counts: dict[str, int] = {}
    if meta:
        extra = meta.get("extra")
        if isinstance(extra, dict) and "note_counts" in extra:
            recorded_counts = extra["note_counts"]
        elif "note_counts" in meta:
            recorded_counts = meta["note_counts"]

    for stem_name, required in [
        (PIANO_STEM_NAME, require_piano),
        (BASS_STEM_NAME, require_bass),
        (VOCALS_STEM_NAME, require_vocals),
        (GUITAR_STEM_NAME, require_guitar),
        (OTHER_STEM_NAME, require_other),
    ]:
        if not required:
            continue
        part = score.find_part(stem_name)
        if part is None:
            return False
        if stem_name in recorded_counts:
            # 当時1音以上採譜されていたのに現在0音なら手動削除とみなし再採譜
            if recorded_counts[stem_name] > 0 and len(part.notes) == 0:
                return False
        else:
            # メタデータ未記録時の後方互換: 1音以上を必須
            if len(part.notes) == 0:
                return False

    return True


def _piano_part_has_notes(workspace_dir: Path, project_id: str) -> bool:
    """後方互換用エイリアス(#24)。"""
    return _transcribe_artifacts_exist(workspace_dir, project_id, require_piano=True)


def run_transcribe_stage(job_id: str, project_id: str, workspace_dir: Path, params: dict) -> None:
    """#24/#54/#55/#56: Stage 3 ピアノ・ベース・ボーカル・ギター/その他AMT。

    §6: 入力(piano/bass/vocals/guitar/otherステム)+使用ライブラリのバージョンが
    不変ならスキップする(separate/beatステージと対称)。**この段階では一切
    ノートを捨てない**: `run_piano_transcription` / `run_bass_transcription` /
    `run_vocals_transcription` / `run_guitar_transcription` が返す
    `ghost_candidate` フラグはそのまま `Note.flags` へ引き継ぎ、削除はしない。

    guitar/other(#56, Basic Pitch ONNX)は既知の制約として、bass/vocalsと同様に
    `voice=1`/`staff=1`固定で書き込む。ポリフォニックな和音を含みうるため、
    ピアノのような`quantize`ステージでの声部/五線の再割り当て
    (`refine_baseline`)は本PRの対象外(#60の実曲検証で問題が見つかり次第、
    個別Issueとして起票する運用方針、設計書§16 M6完了条件を参照)。
    """
    emit(
        {
            "job_id": job_id,
            "stage": "transcribe",
            "progress": 0.0,
            "message": "transcribing piano/bass/vocals/guitar/other",
        }
    )

    force = params.get("force", False)
    stems_dir_path = storage.stems_dir(workspace_dir, project_id)
    piano_stem_path = stems_dir_path / f"{PIANO_STEM_NAME}.wav"
    bass_stem_path = stems_dir_path / f"{BASS_STEM_NAME}.wav"
    vocals_stem_path = stems_dir_path / f"{VOCALS_STEM_NAME}.wav"
    guitar_stem_path = stems_dir_path / f"{GUITAR_STEM_NAME}.wav"
    other_stem_path = stems_dir_path / f"{OTHER_STEM_NAME}.wav"

    has_piano = piano_stem_path.exists()
    has_bass = bass_stem_path.exists()
    has_vocals = vocals_stem_path.exists()
    has_guitar = guitar_stem_path.exists()
    has_other = other_stem_path.exists()

    if not has_piano and not has_bass and not has_vocals and not has_guitar and not has_other:
        # #16: これらのステムは `standard` プリセット(htdemucs_6s)でのみ生成される。
        # `fast`/`high_quality`(4ステム)には無い(M2時点の既知の制約)。
        raise ValueError(
            "no transcribable stems (piano/bass/vocals/guitar/other) found; "
            "run the separate stage with the 'standard' preset first "
            "(only htdemucs_6s produces these dedicated stems)"
        )

    hash_dict: dict[str, Any] = {}
    provider_versions: dict[str, str] = {}

    if has_piano:
        piano_transcription_inference_version = _package_version("piano_transcription_inference")
        hash_dict["audio_fingerprint"] = audio_fingerprint(piano_stem_path)
        hash_dict["package_version"] = piano_transcription_inference_version
        provider_versions["piano_transcription_inference"] = piano_transcription_inference_version

    if has_bass:
        librosa_version = _package_version("librosa")
        scipy_version = _package_version("scipy")
        hash_dict["bass_audio_fingerprint"] = audio_fingerprint(bass_stem_path)
        hash_dict["bass_algo_version"] = BASS_ALGO_VERSION
        hash_dict["bass_librosa_version"] = librosa_version
        hash_dict["bass_scipy_version"] = scipy_version
        provider_versions["bass_transcription"] = BASS_ALGO_VERSION
        provider_versions["librosa"] = librosa_version
        provider_versions["scipy"] = scipy_version

    if has_vocals:
        torchcrepe_version = _package_version("torchcrepe")
        torch_version = _package_version("torch")
        hash_dict["vocals_audio_fingerprint"] = audio_fingerprint(vocals_stem_path)
        hash_dict["vocals_algo_version"] = VOCALS_ALGO_VERSION
        hash_dict["vocals_torchcrepe_version"] = torchcrepe_version
        hash_dict["vocals_torch_version"] = torch_version
        provider_versions["vocals_transcription"] = VOCALS_ALGO_VERSION
        provider_versions["torchcrepe"] = torchcrepe_version
        provider_versions["torch"] = torch_version

    if has_guitar or has_other:
        # guitar/otherは同一の`run_guitar_transcription`(onnxruntime)を共有するため、
        # バージョン取得・`provider_versions`への記録は1回に集約する(#56レビュー
        # 指摘: 個別に代入すると同じキーへの重複書き込みになり、コメントと実装が
        # 食い違っていた)。
        onnxruntime_version = _package_version("onnxruntime")
        provider_versions["onnxruntime"] = onnxruntime_version

    if has_guitar:
        hash_dict["guitar_audio_fingerprint"] = audio_fingerprint(guitar_stem_path)
        hash_dict["guitar_algo_version"] = GUITAR_ALGO_VERSION
        hash_dict["guitar_onnxruntime_version"] = onnxruntime_version
        provider_versions["guitar_transcription"] = GUITAR_ALGO_VERSION

    if has_other:
        hash_dict["other_audio_fingerprint"] = audio_fingerprint(other_stem_path)
        hash_dict["other_algo_version"] = OTHER_ALGO_VERSION
        hash_dict["other_onnxruntime_version"] = onnxruntime_version
        provider_versions["other_transcription"] = OTHER_ALGO_VERSION

    hash_payload = json.dumps(hash_dict, sort_keys=True)
    hash_value = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    if not force and storage.should_skip_stage(
        workspace_dir,
        project_id,
        "transcribe",
        hash_value,
        artifacts_exist=lambda _meta: _transcribe_artifacts_exist(
            workspace_dir,
            project_id,
            require_piano=has_piano,
            require_bass=has_bass,
            require_vocals=has_vocals,
            require_guitar=has_guitar,
            require_other=has_other,
            meta=_meta,
        ),
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
    # 必ず `run_piano_transcription` / `run_bass_transcription` の呼び出しより前に行うこと
    # (#24-M2レビューラウンドで、推論後に読んでしまいレース窓を検出できていなかった
    # 実装ミスを修正した経緯があるため、推論前に読む設計を厳格に維持する、#124レビュー指摘)。
    # M2時点ではScore IRを書き換える経路がこのステージ自身以外に無いため実際には
    # 発火しないが、M3で編集APIが入った際にも安全側に倒れる設計として先に
    # 用意しておく。
    raw_before = storage.read_json(score_path) if score_path.exists() else None

    score = score_service.read_score_optional(project_id) or _initial_score_ir(
        project_id, workspace_dir
    )
    new_piano_notes_count = 0
    piano_pedals_count = 0
    new_bass_notes_count = 0
    new_vocals_notes_count = 0
    new_guitar_notes_count = 0
    new_other_notes_count = 0

    if has_piano:
        result = run_piano_transcription(piano_stem_path)
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
        new_piano_notes_count = len(new_notes)
        piano_pedals_count = len(part.pedals)

    if has_bass:
        result_bass = run_bass_transcription(bass_stem_path)
        bass_part = score.find_part(BASS_STEM_NAME)
        if bass_part is None:
            bass_part = Part(
                id=BASS_STEM_NAME,
                name="Bass",
                midi_program=33,
                stem_source=f"stems/{BASS_STEM_NAME}.wav",
                staves=1,
                clefs=[Clef(staff=1, sign="F", line=4)],
            )
            score.parts.append(bass_part)

        preserved_bass_notes = [n for n in bass_part.notes if n.provenance != "amt"]
        new_bass_notes = [
            Note(
                id=score.allocate_note_id(),
                onset_sec=event.onset_sec,
                duration_sec=event.duration_sec,
                midi=event.midi,
                velocity=event.velocity,
                voice=1,
                staff=1,
                provenance="amt",
                flags=["ghost_candidate"] if event.ghost_candidate else [],
            )
            for event in result_bass.notes
        ]
        bass_part.notes = preserved_bass_notes + new_bass_notes
        bass_part.pedals = []
        new_bass_notes_count = len(new_bass_notes)

    if has_vocals:
        result_vocals = run_vocals_transcription(vocals_stem_path)
        vocals_part = score.find_part(VOCALS_STEM_NAME)
        if vocals_part is None:
            vocals_part = Part(
                id=VOCALS_STEM_NAME,
                name="Vocals",
                midi_program=53,
                stem_source=f"stems/{VOCALS_STEM_NAME}.wav",
                staves=1,
                clefs=[Clef(staff=1, sign="G", line=2)],
            )
            score.parts.append(vocals_part)

        preserved_vocals_notes = [n for n in vocals_part.notes if n.provenance != "amt"]
        new_vocals_notes = [
            Note(
                id=score.allocate_note_id(),
                onset_sec=event.onset_sec,
                duration_sec=event.duration_sec,
                midi=event.midi,
                velocity=event.velocity,
                voice=1,
                staff=1,
                provenance="amt",
                flags=["ghost_candidate"] if event.ghost_candidate else [],
            )
            for event in result_vocals.notes
        ]
        vocals_part.notes = preserved_vocals_notes + new_vocals_notes
        vocals_part.pedals = []
        new_vocals_notes_count = len(new_vocals_notes)

    if has_guitar:
        result_guitar = run_guitar_transcription(guitar_stem_path)
        guitar_part = score.find_part(GUITAR_STEM_NAME)
        if guitar_part is None:
            guitar_part = Part(
                id=GUITAR_STEM_NAME,
                name="Guitar",
                midi_program=25,  # Acoustic Guitar (steel), §6 Stage3表の代表値
                stem_source=f"stems/{GUITAR_STEM_NAME}.wav",
                staves=1,
                clefs=[Clef(staff=1, sign="G", line=2)],
            )
            score.parts.append(guitar_part)

        preserved_guitar_notes = [n for n in guitar_part.notes if n.provenance != "amt"]
        new_guitar_notes = [
            Note(
                id=score.allocate_note_id(),
                onset_sec=event.onset_sec,
                duration_sec=event.duration_sec,
                midi=event.midi,
                velocity=event.velocity,
                voice=1,
                staff=1,
                provenance="amt",
                flags=["ghost_candidate"] if event.ghost_candidate else [],
            )
            for event in result_guitar.notes
        ]
        guitar_part.notes = preserved_guitar_notes + new_guitar_notes
        guitar_part.pedals = []
        new_guitar_notes_count = len(new_guitar_notes)

    if has_other:
        result_other = run_guitar_transcription(other_stem_path)
        other_part = score.find_part(OTHER_STEM_NAME)
        if other_part is None:
            other_part = Part(
                id=OTHER_STEM_NAME,
                name="Other",
                # demucsの残余ステム(楽器種不明)のため、再生用の暫定値として
                # Acoustic Grand Pianoを既定にする(記譜自体には影響しない、#56)。
                midi_program=0,
                stem_source=f"stems/{OTHER_STEM_NAME}.wav",
                staves=1,
                clefs=[Clef(staff=1, sign="G", line=2)],
            )
            score.parts.append(other_part)

        preserved_other_notes = [n for n in other_part.notes if n.provenance != "amt"]
        new_other_notes = [
            Note(
                id=score.allocate_note_id(),
                onset_sec=event.onset_sec,
                duration_sec=event.duration_sec,
                midi=event.midi,
                velocity=event.velocity,
                voice=1,
                staff=1,
                provenance="amt",
                flags=["ghost_candidate"] if event.ghost_candidate else [],
            )
            for event in result_other.notes
        ]
        other_part.notes = preserved_other_notes + new_other_notes
        other_part.pedals = []
        new_other_notes_count = len(new_other_notes)

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
    # #29: score/current.jsonへ実際に新しい採譜結果を書いた場合のみ下流(quantize)を
    # 無効化する。自ステージのstage_metadataを書く前に呼ぶこと。
    _invalidate_downstream_or_reset(workspace_dir, project_id, "transcribe")
    _reset_undo_history_or_warn(workspace_dir, project_id)
    note_counts: dict[str, int] = {}
    if has_piano:
        p = score.find_part(PIANO_STEM_NAME)
        note_counts[PIANO_STEM_NAME] = len(p.notes) if p else 0
    if has_bass:
        b = score.find_part(BASS_STEM_NAME)
        note_counts[BASS_STEM_NAME] = len(b.notes) if b else 0
    if has_vocals:
        v = score.find_part(VOCALS_STEM_NAME)
        note_counts[VOCALS_STEM_NAME] = len(v.notes) if v else 0
    if has_guitar:
        g = score.find_part(GUITAR_STEM_NAME)
        note_counts[GUITAR_STEM_NAME] = len(g.notes) if g else 0
    if has_other:
        o = score.find_part(OTHER_STEM_NAME)
        note_counts[OTHER_STEM_NAME] = len(o.notes) if o else 0

    storage.write_stage_metadata(
        workspace_dir,
        project_id,
        "transcribe",
        params_hash=hash_value,
        provider_versions=provider_versions,
        extra={"note_counts": note_counts},
    )
    if not has_bass and not has_vocals and not has_guitar and not has_other:
        msg = f"{new_piano_notes_count} notes, {piano_pedals_count} pedal events"
    else:
        parts_summary = []
        if has_piano:
            parts_summary.append(f"{new_piano_notes_count} piano")
        if has_bass:
            parts_summary.append(f"{new_bass_notes_count} bass")
        if has_vocals:
            parts_summary.append(f"{new_vocals_notes_count} vocals")
        if has_guitar:
            parts_summary.append(f"{new_guitar_notes_count} guitar")
        if has_other:
            parts_summary.append(f"{new_other_notes_count} other")
        total_notes = (
            new_piano_notes_count
            + new_bass_notes_count
            + new_vocals_notes_count
            + new_guitar_notes_count
            + new_other_notes_count
        )
        msg = f"{total_notes} notes ({', '.join(parts_summary)}), {piano_pedals_count} pedal events"
    emit(
        {
            "job_id": job_id,
            "stage": "transcribe",
            "progress": 1.0,
            "message": msg,
        }
    )


def _piano_notes_are_quantized(workspace_dir: Path, project_id: str) -> bool:
    """`should_skip_stage` の `artifacts_exist` 用(#25/#26、`_piano_part_has_notes`

    と同じ保護パターン): Score IR自体、`piano`パート、またはノートが無くなって
    いればスキップを拒否する。加えて、量子化またはL0が未完了(`onset_tick`が
    未設定、または非userノートで`spelling`が未設定のまま残っている)状態も
    再実行させる(手動でのscore/current.json編集や、以前の実行が途中で
    中断した場合の保護。#25-M2レビュー指摘: `onset_tick`だけを見ると、L0が
    未適用のまま(spelling等が欠けたまま)でもスキップしてしまい、本ステージの
    docstringが謳う「常にエクスポート可能な状態」を守れない)。
    """
    score = ScoreService(workspace_dir=workspace_dir).read_score_optional(project_id)
    if score is None:
        return False
    part = score.find_part(PIANO_STEM_NAME)
    if part is None:
        return False
    active_notes = [n for n in part.notes if n.status != "deleted"]
    if not active_notes:
        return False
    return all(
        n.onset_tick is not None and (n.provenance == "user" or n.spelling is not None)
        for n in active_notes
    )


def run_quantize_stage(job_id: str, project_id: str, workspace_dir: Path, params: dict) -> None:
    """#25/#26: Stage 4 決定論的クオンタイズ + L0決定論的整音(ステージ末尾で実行)。

    L0を独立ステージにせず本ステージの末尾で実行するのは、量子化直後に必ず
    整音済み(=常にエクスポート可能)な状態を保つため(計画時の技術決定)。

    §6: 入力(beatmap.json + pianoパートの生ノート情報)+music21のバージョンが
    不変ならスキップする(separate/beat/transcribeステージと対称)。`onset_sec`/
    `duration_sec`(生データ)はここでは一切変更しない(§10.1)。

    既知の制約(M2時点でuserノート編集APIが未実装のため未検証の簡略化):
    `onset_tick`/`duration_tick`/`snap_candidates`/`selected_snap` は
    `provenance == "user"` かどうかに関わらず全ノートで再計算する(テンポマップ
    修正後の再量子化を可能にするため)。L0(spelling/voice/staff/flags)のみ
    userノートを変更しない。「ユーザーが手動で選んだ`selected_snap`を再量子化で
    上書きしない」という、より細かい保護はM3の編集APIと合わせて実装する。
    """
    force = params.get("force", False)
    emit({"job_id": job_id, "stage": "quantize", "progress": 0.0, "message": "quantizing"})

    beatmap_file = storage.beatmap_path(workspace_dir, project_id)
    if not beatmap_file.exists():
        raise ValueError("beatmap.json not found; run the beat stage first")
    beatmap = storage.read_json(beatmap_file)

    score_service = ScoreService(workspace_dir=workspace_dir)
    score_path = storage.score_current_path(workspace_dir, project_id)
    if not score_path.exists():
        raise ValueError("score/current.json not found; run the transcribe stage first")
    # 楽観的並行性制御(#20/#24と同じ思想)の起点となるスナップショットを、
    # ここで一度だけ読む(#25-M2レビュー指摘: これより後で改めてScore IRを
    # 読み直すと、その間に他プロセスが書き込んだ変更を`raw_before`が正しい
    # 基準点として捉えられず、比較をすり抜けてlost-updateになる)。以降、
    # `score`はこの`raw_before`から構築したモデルのみを使い、書き込み直前まで
    # 再読込しない。
    raw_before = storage.read_json(score_path)
    score = ScoreIR.model_validate(migrate_to_current(raw_before))
    part = score.find_part(PIANO_STEM_NAME)
    if part is None or not part.notes:
        raise ValueError("piano part has no notes; run the transcribe stage first")

    music21_version = _package_version("music21")
    active_notes = [n for n in part.notes if n.status != "deleted"]
    # ハッシュの対象は「このステージへの入力」となる生フィールドのみ(#25-M2
    # レビューと同種の注意点): onset_tick/duration_tick/spelling等はこのステージ
    # 自身が書き込む出力なので含めない。含めると自分の前回出力のせいで毎回
    # ハッシュが変わり続け、スキップが永久に効かなくなってしまう。
    notes_snapshot = [
        {
            "id": n.id,
            "onset_sec": n.onset_sec,
            "duration_sec": n.duration_sec,
            "midi": n.midi,
            "velocity": n.velocity,
            "confidence": n.confidence,
            "provenance": n.provenance,
        }
        for n in active_notes
    ]
    pedals_snapshot = [{"start_sec": p.start_sec, "stop_sec": p.stop_sec} for p in part.pedals]
    hash_payload = json.dumps(
        {
            "beats": beatmap.get("beats", []),
            "time_signatures": beatmap.get("time_signatures", []),
            "notes": notes_snapshot,
            "pedals": pedals_snapshot,
            "divisions": score.divisions,
            "music21_version": music21_version,
        },
        sort_keys=True,
    )
    hash_value = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    if not force and storage.should_skip_stage(
        workspace_dir,
        project_id,
        "quantize",
        hash_value,
        artifacts_exist=lambda _meta: _piano_notes_are_quantized(workspace_dir, project_id),
    ):
        emit(
            {
                "job_id": job_id,
                "stage": "quantize",
                "progress": 1.0,
                "message": "skipped (unchanged input)",
            }
        )
        return

    onset_inputs = [(n.id, n.onset_sec, n.duration_sec) for n in active_notes]
    quantized = quantize_note_onsets(
        onset_inputs,
        beatmap.get("beats", []),
        beatmap.get("time_signatures", []),
        divisions=score.divisions,
        top_n=DEFAULT_TOP_N,
    )
    refine_inputs = [
        RefineNoteInput(
            id=n.id,
            midi=n.midi,
            onset_tick=quantized[n.id].onset_tick,
            duration_sec=n.duration_sec,
            velocity=n.velocity,
            confidence=n.confidence,
            flags=tuple(n.flags),
            is_user=(n.provenance == "user"),
        )
        for n in active_notes
    ]
    refined = refine_baseline(refine_inputs)

    for note in active_notes:
        q = quantized[note.id]
        note.onset_tick = q.onset_tick
        note.duration_tick = q.duration_tick
        note.snap_candidates = [
            SnapCandidate(id=c.id, resolution=c.resolution, tick=c.tick, score=c.score)
            for c in q.snap_candidates
        ]
        note.selected_snap = q.selected_snap

        r = refined.get(note.id)
        if r is None:
            continue  # provenance="user"のノート(#29): L0は変更しない
        step, alter, octave = r.spelling
        note.spelling = Spelling(step=step, alter=alter, octave=octave)
        note.voice = r.voice
        note.staff = r.staff
        note.flags = list(r.flags)

    if part.pedals:
        # #27のMusicXML書き出しがtick位置を必要とするため、ここで併せて
        # 変換しておく(pedalはノートと違いスナップ格子への丸めは行わない)。
        pedal_ticks = quantize_pedal_ticks(
            [(p.start_sec, p.stop_sec) for p in part.pedals],
            beatmap.get("beats", []),
            beatmap.get("time_signatures", []),
            divisions=score.divisions,
        )
        for pedal, (start_tick, stop_tick) in zip(part.pedals, pedal_ticks, strict=True):
            pedal.start_tick = start_tick
            pedal.stop_tick = stop_tick

    # #57 FR-16: コード進行の自動推定とScoreIR.chordsへの格納
    # 下記の楽観的並行性チェック(raw_now != raw_before)を通過後、直後の
    # score_service.write_score(project_id, score) (922行目)によって
    # score/current.json へ永続化される。ScoreService.ensure_chords() を
    # 用いて score.chords を最新ノート情報から確定させておく。
    ScoreService.ensure_chords(score, force_recompute=True)

    raw_now = storage.read_json(score_path) if score_path.exists() else None
    if raw_now != raw_before:
        emit(
            {
                "job_id": job_id,
                "stage": "quantize",
                "progress": 1.0,
                "message": "skipped write: score/current.json was modified concurrently; "
                "not overwriting",
            }
        )
        return

    score_service.write_score(project_id, score)
    _reset_undo_history_or_warn(workspace_dir, project_id)
    storage.write_stage_metadata(
        workspace_dir,
        project_id,
        "quantize",
        params_hash=hash_value,
        provider_versions={"music21": music21_version},
    )
    emit(
        {
            "job_id": job_id,
            "stage": "quantize",
            "progress": 1.0,
            "message": f"quantized {len(active_notes)} notes",
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
            run_beat_stage(job_id, project_id, workspace_dir, params)
        elif stage == "transcribe":
            run_transcribe_stage(job_id, project_id, workspace_dir, params)
        elif stage == "quantize":
            run_quantize_stage(job_id, project_id, workspace_dir, params)
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
