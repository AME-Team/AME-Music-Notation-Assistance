"""#16: DSP Worker のステージオーケストレーション(実モデルはモックして配管だけ検証)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.infra import storage
from app.services.score_service import ScoreService
from app.worker import dsp_main


def _write_stub_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF....WAVEfmt ")


def _fake_run_separation(stem_names: list[str]):
    def _run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        written = {}
        for name in stem_names:
            path = output_dir / f"{name}.wav"
            _write_stub_wav(path)
            written[name] = path
        return written

    return _run


def _setup_project(workspace_dir: Path, project_id: str) -> None:
    storage.ensure_project_layout(workspace_dir, project_id)
    (workspace_dir / project_id / "source.wav").write_bytes(b"RIFF....WAVEfmt ")


def _write_valid_wav(
    path: Path, *, num_frames: int = 800, sample_rate: int = 8000
) -> None:
    """`sf.info()` で実際にパースできる最小限の有効なWAVを書く(#24-M2)。

    `_write_stub_wav` はヘッダの断片だけの意図的に不正なファイルで、
    separate/beatステージのテスト(`audio_fingerprint` はバイト列を読むだけで
    音声としてパースしない)には十分だが、transcribeステージは Score IR 初期化時に
    `soundfile.info()` で原曲を実際にパースする(#23)ため、こちらが必要。
    """
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * num_frames)


def _setup_project_with_valid_source(workspace_dir: Path, project_id: str) -> None:
    storage.ensure_project_layout(workspace_dir, project_id)
    _write_valid_wav(workspace_dir / project_id / "source.wav")


def test_switching_preset_removes_stale_stems_and_their_peaks_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#16/#21 回帰: standard(6stem)→fast(4stem)に切り替えても孤児ステムが残ってはいけない。"""
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    six_stems = ["drums", "bass", "other", "vocals", "guitar", "piano"]
    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(six_stems))
    dsp_main.run_separate_stage(
        "job1",
        project_id,
        tmp_path,
        {"preset": "standard", "execution_provider": "cpu"},
    )

    # 6ステム分のピークキャッシュが存在する状態を作っておく(実際にはAPI経由で作られる)。
    for name in six_stems:
        storage.write_json(
            storage.peaks_path(tmp_path, project_id, name), {"peaks": []}
        )

    four_stems = ["drums", "bass", "other", "vocals"]
    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(four_stems))
    dsp_main.run_separate_stage(
        "job2", project_id, tmp_path, {"preset": "fast", "execution_provider": "cpu"}
    )

    stems_dir = storage.stems_dir(tmp_path, project_id)
    assert {p.stem for p in stems_dir.glob("*.wav")} == set(four_stems)
    assert not storage.peaks_path(tmp_path, project_id, "guitar").exists()
    assert not storage.peaks_path(tmp_path, project_id, "piano").exists()
    # 残ったステムのキャッシュは(再分離されているので)無効化されているべき。
    assert not storage.peaks_path(tmp_path, project_id, "drums").exists()


def test_rerunning_same_preset_skips_and_keeps_all_stems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    stems = ["drums", "bass", "other", "vocals", "guitar", "piano"]
    call_count = 0

    def _run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        nonlocal call_count
        call_count += 1
        return _fake_run_separation(stems)(
            audio_path, output_dir, preset=preset, execution_provider=execution_provider
        )

    monkeypatch.setattr(dsp_main, "run_separation", _run)
    params = {"preset": "standard", "execution_provider": "cpu"}
    dsp_main.run_separate_stage("job1", project_id, tmp_path, params)
    dsp_main.run_separate_stage("job2", project_id, tmp_path, params)

    assert call_count == 1  # 2回目は params_hash が一致しスキップされる
    stems_dir = storage.stems_dir(tmp_path, project_id)
    assert {p.stem for p in stems_dir.glob("*.wav")} == set(stems)


def test_separate_stage_reruns_if_metadata_predates_artifact_names_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): この機能を追加する前に書かれた

    (`artifact_names`キーの無い)旧メタデータに対して、`set() <= 任意の集合`が
    vacuous truthで常にTrueになる穴を突かれないよう、キー欠如時はfail-closed
    (スキップしない)にするべき。再実行後は新形式で自己修復する。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    stems = ["drums", "bass", "other", "vocals"]
    call_count = 0

    def _run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        nonlocal call_count
        call_count += 1
        return _fake_run_separation(stems)(
            audio_path, output_dir, preset=preset, execution_provider=execution_provider
        )

    monkeypatch.setattr(dsp_main, "run_separation", _run)
    params = {"preset": "standard", "execution_provider": "cpu"}
    dsp_main.run_separate_stage("job1", project_id, tmp_path, params)
    assert call_count == 1

    # このステージのメタデータを、artifact_namesの無い旧形式に書き換える。
    meta_path = storage.stage_metadata_path(tmp_path, project_id, "separate")
    meta = storage.read_json(meta_path)
    del meta["artifact_names"]
    storage.write_json(meta_path, meta)

    dsp_main.run_separate_stage("job2", project_id, tmp_path, params)
    assert call_count == 2  # fail-closedで再実行された

    # 自己修復: 3回目は新形式のメタデータでスキップされる。
    dsp_main.run_separate_stage("job3", project_id, tmp_path, params)
    assert call_count == 2


def test_rerunning_same_preset_skips_despite_unrelated_stray_wav_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): artifacts_existは部分集合(`<=`)判定

    であり完全一致(`==`)ではないため、期待した成果物とは無関係な余分な.wav
    (例: 別プリセット実行が強制終了された際の残骸)がステムディレクトリに
    残っていても、期待成果物自体が全て揃っていればスキップされるべき。完全
    一致に戻すリファクタが将来意図せず行われた場合に検出できるよう固定化する。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    stems = ["drums", "bass", "other", "vocals"]
    call_count = 0

    def _run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        nonlocal call_count
        call_count += 1
        return _fake_run_separation(stems)(
            audio_path, output_dir, preset=preset, execution_provider=execution_provider
        )

    monkeypatch.setattr(dsp_main, "run_separation", _run)
    params = {"preset": "standard", "execution_provider": "cpu"}
    dsp_main.run_separate_stage("job1", project_id, tmp_path, params)
    assert call_count == 1

    # 無関係な余分な.wavを置く(例えば別プリセット実行の残骸を模擬する)。
    stems_dir = storage.stems_dir(tmp_path, project_id)
    _write_stub_wav(stems_dir / "unexpected_leftover.wav")

    dsp_main.run_separate_stage("job2", project_id, tmp_path, params)
    assert call_count == 1  # 余分ファイルの存在だけではスキップ最適化が壊れない


def test_separate_stage_reruns_if_stems_deleted_despite_matching_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#21-M1レビュー指摘): メタデータのハッシュが一致していても、ステムが

    (手動削除や、ジョブ強制終了で `except BaseException` クリーンアップが走らず
    メタデータだけ残ったケースを想定して)存在しなければスキップせず再実行する。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    stems = ["drums", "bass", "other", "vocals"]
    call_count = 0

    def _run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        nonlocal call_count
        call_count += 1
        return _fake_run_separation(stems)(
            audio_path, output_dir, preset=preset, execution_provider=execution_provider
        )

    monkeypatch.setattr(dsp_main, "run_separation", _run)
    params = {"preset": "fast", "execution_provider": "cpu"}
    dsp_main.run_separate_stage("job1", project_id, tmp_path, params)
    assert call_count == 1

    # ステムを手動削除する(メタデータは残ったまま)。
    stems_dir = storage.stems_dir(tmp_path, project_id)
    for path in stems_dir.glob("*.wav"):
        path.unlink()

    dsp_main.run_separate_stage("job2", project_id, tmp_path, params)
    assert call_count == 2  # ハッシュ一致でもスキップされず再実行された
    assert {p.stem for p in stems_dir.glob("*.wav")} == set(stems)


def test_separate_stage_reruns_if_only_some_stems_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): 全ステムではなく一部だけ(例:

    vocals.wavのみ)手動削除された場合も、「1つでも存在すればOK」という緩い
    判定では見逃してしまう。write_stage_metadataに記録した期待ステム名が
    ディスク上の集合に全て含まれるか(部分集合として)まで確認し、欠けていれば
    再実行させるべき。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    stems = ["drums", "bass", "other", "vocals"]
    call_count = 0

    def _run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        nonlocal call_count
        call_count += 1
        return _fake_run_separation(stems)(
            audio_path, output_dir, preset=preset, execution_provider=execution_provider
        )

    monkeypatch.setattr(dsp_main, "run_separation", _run)
    params = {"preset": "standard", "execution_provider": "cpu"}
    dsp_main.run_separate_stage("job1", project_id, tmp_path, params)
    assert call_count == 1

    # vocalsだけ手動削除する(他の3ステムとメタデータは残ったまま)。
    stems_dir = storage.stems_dir(tmp_path, project_id)
    (stems_dir / "vocals.wav").unlink()

    dsp_main.run_separate_stage("job2", project_id, tmp_path, params)
    assert call_count == 2  # 3ステムが残っていてもスキップされず再実行された
    assert {p.stem for p in stems_dir.glob("*.wav")} == set(stems)


def test_separate_stage_records_resolved_model_not_preset_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-11回帰: メタデータには("standard"等のUIラベルではなく)実モデル名を記録する。"""
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(["drums"]))
    dsp_main.run_separate_stage(
        "job1",
        project_id,
        tmp_path,
        {"preset": "standard", "execution_provider": "cpu"},
    )

    meta = storage.read_json(
        storage.stage_metadata_path(tmp_path, project_id, "separate")
    )
    assert meta["versions"]["model"] == "htdemucs_6s"
    assert meta["versions"]["preset"] == "standard"


def test_beat_stage_skips_when_audio_unchanged_preserving_manual_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#18回帰: separateと対称にskipし、BeatGridEditorの手動補正を誤って上書きしない。"""
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    call_count = 0

    class _FakeResult:
        def __init__(self) -> None:
            # インスタンス属性にする(#21-M1レビュー指摘の追加ラウンド): クラス
            # 属性の可変デフォルトはテスト間で共有されうるため、将来この値を
            # 書き換えるテストが増えた際に意図しない状態漏れの温床になる。
            self.beats: list = []
            self.downbeats_sec: list = []

        def to_dict(self) -> dict:
            return {
                "beats": [],
                "downbeats_sec": [],
                "time_signatures": [],
                "tempo_map": [],
                "confidence": 0.0,
            }

    def _fake_run_beat_estimation(audio_path: str) -> _FakeResult:
        nonlocal call_count
        call_count += 1
        return _FakeResult()

    monkeypatch.setattr(dsp_main, "run_beat_estimation", _fake_run_beat_estimation)
    dsp_main.run_beat_stage("job1", project_id, tmp_path, {})

    # ユーザーがBeatGridEditorで手動補正したと仮定する。
    beatmap_path = storage.beatmap_path(tmp_path, project_id)
    beatmap = storage.read_json(beatmap_path)
    beatmap["source"] = "manual"
    beatmap["downbeats_sec"] = [1.23]
    storage.write_json(beatmap_path, beatmap)

    # 音源を変えずに再実行 → スキップされ、手動補正が生き残る。
    dsp_main.run_beat_stage("job2", project_id, tmp_path, {})

    assert call_count == 1
    assert storage.read_json(beatmap_path)["source"] == "manual"
    assert storage.read_json(beatmap_path)["downbeats_sec"] == [1.23]


def test_beat_stage_reruns_if_beatmap_deleted_despite_matching_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#21-M1レビュー指摘): メタデータのハッシュが一致していても、beatmap.json

    が(手動削除等で)存在しなければスキップせず再実行する。スキップし続けると
    GET /analysis/beatmap が404を返し続け、再実行しても復旧しない事故になる。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)
    call_count = 0

    class _FakeResult:
        def __init__(self) -> None:
            # インスタンス属性にする(#21-M1レビュー指摘の追加ラウンド): クラス
            # 属性の可変デフォルトはテスト間で共有されうるため、将来この値を
            # 書き換えるテストが増えた際に意図しない状態漏れの温床になる。
            self.beats: list = []
            self.downbeats_sec: list = []

        def to_dict(self) -> dict:
            return {
                "beats": [],
                "downbeats_sec": [],
                "time_signatures": [],
                "tempo_map": [],
                "confidence": 0.0,
            }

    def _fake_run_beat_estimation(audio_path: str) -> _FakeResult:
        nonlocal call_count
        call_count += 1
        return _FakeResult()

    monkeypatch.setattr(dsp_main, "run_beat_estimation", _fake_run_beat_estimation)
    dsp_main.run_beat_stage("job1", project_id, tmp_path, {})
    assert call_count == 1

    storage.beatmap_path(tmp_path, project_id).unlink()

    dsp_main.run_beat_stage("job2", project_id, tmp_path, {})
    assert call_count == 2  # ハッシュ一致でもスキップされず再実行された
    assert storage.beatmap_path(tmp_path, project_id).exists()


def test_separation_failure_invalidates_peaks_cache_for_old_and_partial_stems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#16レビュー指摘): 分離が途中で失敗しても、新旧どちらのステム名の

    ピークキャッシュも無効化される(ファイル自体の完全な原子性は既知の制約だが、
    少なくとも矛盾した波形データをUIに残さない)。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    # 1回目: standardプリセットで6ステムを正常に分離しておく。
    six_stems = ["drums", "bass", "other", "vocals", "guitar", "piano"]
    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(six_stems))
    dsp_main.run_separate_stage(
        "job1",
        project_id,
        tmp_path,
        {"preset": "standard", "execution_provider": "cpu"},
    )
    for name in six_stems:
        storage.write_json(
            storage.peaks_path(tmp_path, project_id, name), {"peaks": []}
        )

    # 2回目: fastプリセットへの切替中に、一部のステムだけ書いて例外を投げる
    # (ループ途中の失敗を模擬する)。
    def _failing_run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        _write_stub_wav(output_dir / "drums.wav")  # 新ステムを1つだけ書く
        raise RuntimeError("simulated mid-separation failure")

    monkeypatch.setattr(dsp_main, "run_separation", _failing_run)
    with pytest.raises(RuntimeError, match="simulated mid-separation failure"):
        dsp_main.run_separate_stage(
            "job2",
            project_id,
            tmp_path,
            {"preset": "fast", "execution_provider": "cpu"},
        )

    # 新旧どちらのステム名についても、古いピークキャッシュは残っていない。
    for name in six_stems:
        assert not storage.peaks_path(tmp_path, project_id, name).exists()


def test_separation_failure_clears_stage_metadata_forcing_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#20-M1レビュー指摘): standardで成功→fastへの切替が途中失敗して混在

    ステムが残った状態で、standardを再実行したときにparams_hashが前回成功時と
    一致してスキップされてはいけない(混在した破損ステムがそのまま配信され続ける)。
    stage_metadataを失敗時に消しておき、次回は必ず再実行させる。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)

    six_stems = ["drums", "bass", "other", "vocals", "guitar", "piano"]
    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(six_stems))
    standard_params = {"preset": "standard", "execution_provider": "cpu"}
    dsp_main.run_separate_stage("job1", project_id, tmp_path, standard_params)

    def _failing_run(
        audio_path: Path, output_dir: Path, *, preset: str, execution_provider: str
    ):
        _write_stub_wav(output_dir / "drums.wav")
        raise RuntimeError("simulated mid-separation failure")

    monkeypatch.setattr(dsp_main, "run_separation", _failing_run)
    with pytest.raises(RuntimeError, match="simulated mid-separation failure"):
        dsp_main.run_separate_stage(
            "job2",
            project_id,
            tmp_path,
            {"preset": "fast", "execution_provider": "cpu"},
        )

    # standardへ再実行したとき、失敗前のstandardハッシュと一致してスキップされない。
    call_count = 0
    real_run = _fake_run_separation(six_stems)

    def _counting_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "run_separation", _counting_run)
    dsp_main.run_separate_stage("job3", project_id, tmp_path, standard_params)
    assert call_count == 1  # スキップされず実際に再実行された


def test_beat_stage_skips_write_if_beatmap_modified_during_estimation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#20レビュー指摘): 推論中にBeatGridEditorのPATCHが割り込んだ場合、

    lost-updateで手動補正を上書きしない(楽観的並行性制御: 書き込み直前に
    現在の `source` フィールドを直接確認する方式。mtime比較は同一tick内の
    変更を検出できないことがあるため使わない)。
    """
    project_id = "proj_test"
    _setup_project(tmp_path, project_id)
    beatmap_path = storage.beatmap_path(tmp_path, project_id)

    # 先に一度実行し、beatmap.jsonを存在させる(sourceフィールド確認の前提)。
    class _FakeResult:
        def __init__(self) -> None:
            # インスタンス属性にする(#21-M1レビュー指摘の追加ラウンド): クラス
            # 属性の可変デフォルトはテスト間で共有されうるため、将来この値を
            # 書き換えるテストが増えた際に意図しない状態漏れの温床になる。
            self.beats: list = []
            self.downbeats_sec: list = []

        def to_dict(self) -> dict:
            return {
                "beats": [],
                "downbeats_sec": [],
                "time_signatures": [],
                "tempo_map": [],
                "confidence": 0.0,
            }

    monkeypatch.setattr(
        dsp_main, "run_beat_estimation", lambda _audio_path: _FakeResult()
    )
    dsp_main.run_beat_stage("job1", project_id, tmp_path, {})

    # 音源を変えて(=フィンガープリントを変えて)2回目を「実行」させつつ、
    # 推論中に別プロセス(=BeatGridEditorのPATCH)がbeatmap.jsonを書き換えたと
    # 模擬する。推論関数の呼び出し自体をフックして、その最中にファイルを
    # 更新してからダミー結果を返す。
    (tmp_path / project_id / "source.wav").write_bytes(
        b"RIFF....WAVEfmt X"
    )  # audio変更

    def _run_and_concurrently_patch(_audio_path: str) -> _FakeResult:
        manual_beatmap = {
            "beats": [],
            "downbeats_sec": [9.99],
            "time_signatures": [],
            "tempo_map": [],
            "confidence": 0.0,
            "source": "manual",
        }
        storage.write_json(beatmap_path, manual_beatmap)
        return _FakeResult()

    monkeypatch.setattr(dsp_main, "run_beat_estimation", _run_and_concurrently_patch)
    dsp_main.run_beat_stage("job2", project_id, tmp_path, {})

    # beatステージ自身の書き込みはスキップされ、"PATCH" の内容が生き残る。
    final = storage.read_json(beatmap_path)
    assert final["source"] == "manual"
    assert final["downbeats_sec"] == [9.99]

    # 回帰(#20-M1レビュー指摘の追加ラウンド): 書き込みはスキップされても
    # stage_metadataは今回の(新しい)音源のハッシュで更新されているべき。
    # 更新されないと、同じ音源で再実行するたびに毎回フル推論が走ってしまう。
    call_count = 0

    def _counting_run(_audio_path: str) -> _FakeResult:
        nonlocal call_count
        call_count += 1
        return _FakeResult()

    monkeypatch.setattr(dsp_main, "run_beat_estimation", _counting_run)
    dsp_main.run_beat_stage("job3", project_id, tmp_path, {})
    assert call_count == 0  # 音源不変なのでスキップされ、推論は再実行されない
    assert storage.read_json(beatmap_path)["source"] == "manual"  # 手動補正は保持


# --- #24: run_transcribe_stage --------------------------------------------------


def _fake_transcription_result(
    notes: list[tuple[float, float, int, int, bool]], pedals=()
):
    """`(onset_sec, duration_sec, midi, velocity, ghost_candidate)` のタプルから

    `TranscriptionResult` を組み立てる、テスト用の `run_piano_transcription` フェイク。
    """
    from app.pipeline.transcribe.piano import NoteEvent, PedalEvent, TranscriptionResult

    def _run(_audio_path: Path):
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=onset,
                    duration_sec=duration,
                    midi=midi,
                    velocity=velocity,
                    ghost_candidate=ghost,
                )
                for onset, duration, midi, velocity, ghost in notes
            ],
            pedals=[PedalEvent(start_sec=s, stop_sec=e) for s, e in pedals],
        )

    return _run


def test_transcribe_stage_creates_score_ir_with_piano_part_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")

    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result(
            [(0.0, 0.5, 60, 90, False), (0.5, 0.02, 64, 90, True)],
            pedals=[(0.0, 1.0)],
        ),
    )

    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})

    from app.services.score_service import ScoreService

    score = ScoreService(workspace_dir=tmp_path).read_score("proj_test")
    part = score.find_part("piano")
    assert part is not None
    assert len(part.notes) == 2
    assert {n.midi for n in part.notes} == {60, 64}
    assert all(n.provenance == "amt" for n in part.notes)
    ghost_note = next(n for n in part.notes if n.midi == 64)
    assert ghost_note.flags == ["ghost_candidate"]  # 削除されず保持される(#24)
    normal_note = next(n for n in part.notes if n.midi == 60)
    assert normal_note.flags == []
    assert len(part.pedals) == 1


def test_transcribe_stage_raises_if_piano_stem_missing(tmp_path: Path) -> None:
    """回帰(#24): pianoステムは`standard`プリセットでのみ生成される。無ければ

    明示的なエラーにする(#16の`resolve_onnx_providers`と同じ設計方針)。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)

    with pytest.raises(ValueError, match="no transcribable stems"):
        dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})


def test_transcribe_stage_skips_when_input_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")

    call_count = 0

    def _counting_transcribe(audio_path: Path):
        nonlocal call_count
        call_count += 1
        return _fake_transcription_result([(0.0, 0.5, 60, 90, False)])(audio_path)

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _counting_transcribe)
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})

    assert call_count == 1  # 2回目はハッシュが一致しスキップされる


def test_transcribe_stage_reruns_if_piano_part_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#24-M2レビュー指摘の想定): Score IRやpianoパートが手動削除されても、

    ハッシュ一致だけでスキップせず再実行する(#21-M1の`artifacts_exist`と同じ保護)。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")

    call_count = 0

    def _counting_transcribe(audio_path: Path):
        nonlocal call_count
        call_count += 1
        return _fake_transcription_result([(0.0, 0.5, 60, 90, False)])(audio_path)

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _counting_transcribe)
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    assert call_count == 1

    storage.score_current_path(tmp_path, project_id).unlink()

    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})
    assert call_count == 2  # ハッシュ一致でもスキップされず再実行された


def test_transcribe_stage_preserves_non_amt_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#29の考え方の先取り): 将来M3で手動編集(provenance="user")が入っても、

    再採譜がそれを消してはいけない。M2時点ではまだ手動編集経路が無いため、
    Score IRを直接書き換えてこの状況を模擬する。
    """
    from app.domain.score import Note
    from app.services.score_service import ScoreService

    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")

    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})

    # ユーザーが手動でノートを追加したと仮定する。
    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    part = score.find_part("piano")
    assert part is not None
    user_note = Note(
        id=score.allocate_note_id(),
        onset_sec=10.0,
        duration_sec=1.0,
        midi=72,
        velocity=100,
        provenance="user",
    )
    part.notes.append(user_note)
    service.write_score(project_id, score)

    # 音源を変えて再採譜させる(スキップさせない)。
    (tmp_path / project_id / "stems" / "piano.wav").write_bytes(b"RIFF....WAVEfmt X")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 62, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})

    final_part = service.read_score(project_id).find_part("piano")
    assert final_part is not None
    provenances = {(n.provenance, n.midi) for n in final_part.notes}
    assert ("user", 72) in provenances  # ユーザーのノートは保持される
    assert ("amt", 62) in provenances  # 新しいAMT結果に差し替わる
    assert ("amt", 60) not in provenances  # 古いAMT結果は消える


def test_transcribe_stage_resets_undo_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#32-M3レビュー指摘): 再採譜はamtノートを新しいIDで作り直すため、

    古いUndoEntryが参照するnote_idが無関係になりうる。再採譜のたびに
    Undo/Redoスタックをクリアする(`score_undo.reset_undo_state`参照)。
    """
    from app.services import score_undo

    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})

    undo_state_path = storage.score_undo_state_path(tmp_path, project_id)
    stale_state = score_undo.UndoState(
        done=[
            score_undo.UndoEntry(
                ops=[{"type": "note.update", "note_ids": [1], "midi": 61}],
                actor="user",
                ts="2026-01-01T00:00:00Z",
                changes={
                    "1": score_undo.NoteChange(
                        part_id="piano", before={"midi": 60}, after={"midi": 61}
                    )
                },
            )
        ]
    )
    score_undo.write_undo_state(undo_state_path, stale_state)

    (tmp_path / project_id / "stems" / "piano.wav").write_bytes(b"RIFF....WAVEfmt X")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 62, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})

    reloaded = score_undo.read_undo_state(undo_state_path)
    assert reloaded.done == []
    assert reloaded.undone == []


def test_transcribe_stage_skips_write_if_score_modified_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#24-M2): 推論中にScore IRが外部から書き換えられた場合、lost-updateで

    上書きしない(beatステージの`source == "manual"`保護と同じ思想の楽観的並行性制御)。
    """
    from app.services.score_service import ScoreService

    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")

    def _transcribe_and_concurrently_modify(audio_path: Path):
        # 推論中に別プロセスがScore IRを書き換えたことを模擬する。
        service = ScoreService(workspace_dir=tmp_path)
        score = service.read_score_optional(project_id) or dsp_main._initial_score_ir(
            project_id, tmp_path
        )
        service.write_score(project_id, score)
        return _fake_transcription_result([(0.0, 0.5, 60, 90, False)])(audio_path)

    monkeypatch.setattr(
        dsp_main, "run_piano_transcription", _transcribe_and_concurrently_modify
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})

    # transcribeステージ自身の書き込みはスキップされ、"割り込み"の内容(pianoパート無し)
    # が生き残る。
    final_score = ScoreService(workspace_dir=tmp_path).read_score(project_id)
    assert final_score.find_part("piano") is None


# --- #25/#26: run_quantize_stage --------------------------------------------------


def _write_beatmap(
    workspace_dir: Path, project_id: str, *, num_beats: int = 16
) -> None:
    """120bpm・4/4のbeatmap.jsonを書く(#25テスト用の最小フィクスチャ)。"""
    beats = [
        {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
        for i in range(num_beats)
    ]
    storage.write_json(
        storage.beatmap_path(workspace_dir, project_id),
        {
            "beats": beats,
            "downbeats_sec": [b["time_sec"] for b in beats if b["beat_in_bar"] == 1],
            "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
            "tempo_map": [],
            "confidence": 1.0,
        },
    )


def test_quantize_stage_raises_if_beatmap_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})

    with pytest.raises(ValueError, match="beatmap.json not found"):
        dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})


def test_quantize_stage_raises_if_score_missing(tmp_path: Path) -> None:
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_beatmap(tmp_path, project_id)

    with pytest.raises(ValueError, match="score/current.json not found"):
        dsp_main.run_quantize_stage("job1", project_id, tmp_path, {})


def test_quantize_stage_raises_if_piano_part_has_no_notes(tmp_path: Path) -> None:
    """回帰(#56): quantizeが処理する全パート("piano"含む)が空の場合のエラー。"""
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_beatmap(tmp_path, project_id)

    service = ScoreService(workspace_dir=tmp_path)
    service.write_score(project_id, dsp_main._initial_score_ir(project_id, tmp_path))

    with pytest.raises(ValueError, match="no part has notes"):
        dsp_main.run_quantize_stage("job1", project_id, tmp_path, {})


def test_quantize_stage_sets_tick_spelling_voice_staff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result(
            [(0.0, 0.5, 60, 90, False), (0.5, 0.5, 64, 90, False)]
        ),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})

    part = (
        ScoreService(workspace_dir=tmp_path).read_score(project_id).find_part("piano")
    )
    assert part is not None
    for note in part.notes:
        assert note.onset_tick is not None
        assert note.duration_tick is not None and note.duration_tick >= 1
        assert note.selected_snap is not None
        assert note.selected_snap in {c.id for c in note.snap_candidates}
        assert note.spelling is not None
        assert note.staff in (1, 2)
        assert 1 <= note.voice <= 4


def test_quantize_stage_sets_tick_and_spelling_for_all_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#56): quantizeがpianoだけでなくbass/vocals/guitar/otherも処理すること。

    #54/#55/#56でbass/vocals/guitar/otherの採譜が実装されて以降、quantizeが
    pianoのみを処理し続けていたため、これらのパートは`onset_tick`/`spelling`が
    一切設定されずMusicXMLエクスポートが常に拒否されるという既存ギャップが
    あった(export/score_builder.pyがNoneを拒否するため)。
    """
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_all_parts"
    _setup_project_with_valid_source(tmp_path, project_id)
    for stem in ["piano", "bass", "vocals", "guitar", "other"]:
        _write_stub_wav(storage.stems_dir(tmp_path, project_id) / f"{stem}.wav")

    def _single_note(midi: int):
        def _mock(_p, **_k):
            return TranscriptionResult(
                notes=[
                    NoteEvent(
                        onset_sec=0.0,
                        duration_sec=0.5,
                        midi=midi,
                        velocity=90,
                        ghost_candidate=False,
                    )
                ],
                pedals=[],
            )

        return _mock

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _single_note(60))
    monkeypatch.setattr(dsp_main, "run_bass_transcription", _single_note(36))
    monkeypatch.setattr(dsp_main, "run_vocals_transcription", _single_note(69))

    def _mock_guitar_or_other(stem_path: Path, **_k):
        midi = 52 if "guitar" in stem_path.name else 64
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=midi,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_guitar_transcription", _mock_guitar_or_other)

    dsp_main.run_transcribe_stage("job_tr", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})

    score = ScoreService(workspace_dir=tmp_path).read_score(project_id)
    for stem_name in ["piano", "bass", "vocals", "guitar", "other"]:
        part = score.find_part(stem_name)
        assert part is not None, f"{stem_name} part missing"
        assert len(part.notes) == 1
        note = part.notes[0]
        assert note.onset_tick is not None, f"{stem_name} onset_tick not set"
        assert note.duration_tick is not None and note.duration_tick >= 1
        assert note.spelling is not None, f"{stem_name} spelling not set"
        assert 1 <= note.voice <= 4

    # ピアノ以外は単一譜表固定(#56): staff=2は割り当てられない。
    for stem_name in ["bass", "vocals", "guitar", "other"]:
        part = score.find_part(stem_name)
        assert part is not None
        assert all(n.staff == 1 for n in part.notes)


def test_quantize_stage_assigns_distinct_voices_to_guitar_chord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#56 Gate2レビュー指摘): guitarの同時発音和音が同一voiceに潰れず、

    高音から低音の順に別voiceへ割り当てられること(単一譜表向けvoice割当)。
    """
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_guitar_chord"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "guitar.wav")

    chord_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=64, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=60, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=52, velocity=80, ghost_candidate=False
        ),
    ]
    monkeypatch.setattr(
        dsp_main,
        "run_guitar_transcription",
        lambda _p, **_k: TranscriptionResult(notes=chord_notes, pedals=[]),
    )
    dsp_main.run_transcribe_stage("job_tr", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})

    part = (
        ScoreService(workspace_dir=tmp_path).read_score(project_id).find_part("guitar")
    )
    assert part is not None
    assert len(part.notes) == 3
    by_midi = {n.midi: n for n in part.notes}
    assert by_midi[64].voice == 1
    assert by_midi[60].voice == 2
    assert by_midi[52].voice == 3
    assert all(n.staff == 1 for n in part.notes)


def test_quantize_stage_resets_undo_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#32-M3レビュー指摘): quantizeは全ノートのonset_tick/duration_tickを

    再計算するため、古いUndoEntryを適用すると再量子化前の位置に巻き戻って
    しまう。再実行のたびにUndo/Redoスタックをクリアする。
    """
    from app.services import score_undo

    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    undo_state_path = storage.score_undo_state_path(tmp_path, project_id)
    stale_state = score_undo.UndoState(
        done=[
            score_undo.UndoEntry(
                ops=[{"type": "note.update", "note_ids": [1], "midi": 61}],
                actor="user",
                ts="2026-01-01T00:00:00Z",
                changes={
                    "1": score_undo.NoteChange(
                        part_id="piano", before={"midi": 60}, after={"midi": 61}
                    )
                },
            )
        ]
    )
    score_undo.write_undo_state(undo_state_path, stale_state)

    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})

    reloaded = score_undo.read_undo_state(undo_state_path)
    assert reloaded.done == []
    assert reloaded.undone == []


def test_quantize_stage_sets_pedal_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#27): ペダルの start_tick/stop_tick も量子化ステージで設定される

    (Stage 6のMusicXML書き出しが`<pedal>`にtick位置を必要とするため)。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)], pedals=[(0.5, 1.0)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})

    part = (
        ScoreService(workspace_dir=tmp_path).read_score(project_id).find_part("piano")
    )
    assert part is not None
    assert len(part.pedals) == 1
    # 120bpm・4/4: 0.5s=1拍=480tick、1.0s=2拍=960tick。
    assert part.pedals[0].start_tick == 480
    assert part.pedals[0].stop_tick == 960


def test_quantize_stage_skips_when_input_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    from app.pipeline.quantize import quantize_note_onsets as _original_quantize

    call_count = 0

    def _counting_quantize(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _original_quantize(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "quantize_note_onsets", _counting_quantize)
    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})
    dsp_main.run_quantize_stage("job3", project_id, tmp_path, {})

    assert call_count == 1  # 2回目はハッシュ一致でスキップされる


def test_quantize_stage_reruns_if_pedals_change_even_if_notes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#27-M2レビュー): params_hashにpedalsを含めた狙いの検証。

    ノートが同じでもペダルだけが変われば再実行され、ペダルのtickも
    最新の値へ更新されるべき。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)], pedals=[(0.0, 0.5)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    from app.pipeline.quantize import quantize_note_onsets as _original_quantize

    call_count = 0

    def _counting_quantize(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _original_quantize(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "quantize_note_onsets", _counting_quantize)
    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})
    assert call_count == 1

    # ノートはそのまま、ペダルの終了時刻だけを書き換える。
    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    part = score.find_part("piano")
    assert part is not None
    part.pedals[0].stop_sec = 1.0
    service.write_score(project_id, score)

    dsp_main.run_quantize_stage("job3", project_id, tmp_path, {})
    assert call_count == 2  # pedals変更によりスキップされず再実行される

    final_part = service.read_score(project_id).find_part("piano")
    assert final_part is not None
    assert final_part.pedals[0].stop_tick == 960  # 1.0s -> 960tick(120bpm)へ更新済み


def test_quantize_stage_reruns_if_notes_not_quantized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰: `onset_tick`が未設定のノートが残っていれば、ハッシュ一致でもスキップしない

    (`_piano_notes_are_quantized`の保護、#21-M1の`artifacts_exist`と同種)。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    from app.pipeline.quantize import quantize_note_onsets as _original_quantize

    call_count = 0

    def _counting_quantize(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _original_quantize(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "quantize_note_onsets", _counting_quantize)
    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})
    assert call_count == 1

    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    part = score.find_part("piano")
    assert part is not None
    part.notes[0].onset_tick = None
    service.write_score(project_id, score)

    dsp_main.run_quantize_stage("job3", project_id, tmp_path, {})
    assert call_count == 2  # ハッシュ一致でもスキップされず再実行された


def test_quantize_stage_does_not_overwrite_user_note_spelling_voice_staff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#29の考え方の先取り): `provenance="user"`のノートのspelling/voice/staffは

    L0が上書きしない(#26)。M2時点ではまだ手動編集経路が無いため、Score IRを
    直接書き換えてこの状況を模擬する。
    """
    from app.domain.score import Note

    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    part = score.find_part("piano")
    assert part is not None
    user_note = Note(
        id=score.allocate_note_id(),
        onset_sec=2.0,
        duration_sec=0.5,
        midi=72,
        velocity=100,
        provenance="user",
        voice=3,
        staff=2,
    )
    part.notes.append(user_note)
    service.write_score(project_id, score)

    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})

    final_part = service.read_score(project_id).find_part("piano")
    assert final_part is not None
    final_user_note = next(n for n in final_part.notes if n.provenance == "user")
    assert final_user_note.spelling is None  # L0は変更しない
    assert final_user_note.voice == 3
    assert final_user_note.staff == 2


def test_quantize_stage_skips_write_if_score_modified_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰: 計算中にScore IRが外部から書き換えられた場合、lost-updateで上書きしない

    (transcribeステージと同じ楽観的並行性制御)。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    from app.pipeline.quantize import quantize_note_onsets as _original_quantize

    def _quantize_and_concurrently_modify(*args, **kwargs):
        service = ScoreService(workspace_dir=tmp_path)
        score = service.read_score(project_id)
        score.meta.stages["_concurrent_marker"] = {"touched": True}
        service.write_score(project_id, score)
        return _original_quantize(*args, **kwargs)

    monkeypatch.setattr(
        dsp_main, "quantize_note_onsets", _quantize_and_concurrently_modify
    )
    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})

    final_score = ScoreService(workspace_dir=tmp_path).read_score(project_id)
    assert final_score.meta.stages.get("_concurrent_marker") == {"touched": True}
    part = final_score.find_part("piano")
    assert part is not None
    assert all(
        n.onset_tick is None for n in part.notes
    )  # quantizeの書き込みはスキップされた


def test_quantize_stage_detects_concurrent_write_before_should_skip_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰(#25-M2レビュー2巡目): `raw_before`のスナップショットとハッシュ計算に

    使うScore IRの読み取りが分離していた旧実装では、その間(`should_skip_stage`
    呼び出し中を含む)に他プロセスが書き込むとlost updateになっていた。読み取りを
    1回に統合した修正後は、`quantize_note_onsets`呼び出しより前に発生した並行書き込み
    も検出できることを確認する(前のテストは`quantize_note_onsets`呼び出し中の
    書き込みしか模擬しておらず、この巡目の指摘は再現できない)。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    original_should_skip_stage = storage.should_skip_stage

    def _should_skip_stage_and_concurrently_modify(*args, **kwargs):
        service = ScoreService(workspace_dir=tmp_path)
        score = service.read_score(project_id)
        score.meta.stages["_concurrent_marker"] = {"touched": True}
        service.write_score(project_id, score)
        return original_should_skip_stage(*args, **kwargs)

    monkeypatch.setattr(
        storage, "should_skip_stage", _should_skip_stage_and_concurrently_modify
    )
    dsp_main.run_quantize_stage("job2", project_id, tmp_path, {})

    final_score = ScoreService(workspace_dir=tmp_path).read_score(project_id)
    assert final_score.meta.stages.get("_concurrent_marker") == {"touched": True}
    part = final_score.find_part("piano")
    assert part is not None
    assert all(
        n.onset_tick is None for n in part.notes
    )  # quantizeの書き込みはスキップされた


# --- #29: force フラグと下流無効化の配線 -----------------------------------------


def _run_full_pipeline_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_id: str
) -> None:
    """separate→transcribe→quantizeを一通り実行し、3ステージ全てのmeta.jsonを作る。"""
    _setup_project_with_valid_source(tmp_path, project_id)
    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(["piano"]))
    dsp_main.run_separate_stage(
        "job_sep",
        project_id,
        tmp_path,
        {"preset": "standard", "execution_provider": "cpu"},
    )
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)
    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})


def test_separate_stage_force_reruns_and_invalidates_downstream_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29: `force=True`はハッシュ一致でも再実行し、実際に書いた場合は下流

    (transcribe/quantize)のmeta.jsonを無効化する。
    """
    project_id = "proj_test"
    _run_full_pipeline_once(tmp_path, monkeypatch, project_id)
    assert storage.stage_metadata_path(tmp_path, project_id, "transcribe").exists()
    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()

    call_count = 0
    real_run = _fake_run_separation(["piano"])

    def _counting_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "run_separation", _counting_run)
    dsp_main.run_separate_stage(
        "job_sep2",
        project_id,
        tmp_path,
        {"preset": "standard", "execution_provider": "cpu", "force": True},
    )
    assert call_count == 1  # forceによりハッシュ一致でも再実行された
    assert not storage.stage_metadata_path(tmp_path, project_id, "transcribe").exists()
    assert not storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()


def test_beat_stage_write_invalidates_quantize_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29: beatmap.jsonへ実際に新しい推論結果を書いた場合、quantizeのmeta.jsonを無効化する。"""
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)
    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})
    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()

    class _FakeResult:
        def __init__(self) -> None:
            self.beats: list = []
            self.downbeats_sec: list = []

        def to_dict(self) -> dict:
            return {
                "beats": [],
                "downbeats_sec": [],
                "time_signatures": [],
                "tempo_map": [],
                "confidence": 0.0,
            }

    monkeypatch.setattr(
        dsp_main, "run_beat_estimation", lambda _audio_path: _FakeResult()
    )
    dsp_main.run_beat_stage("job_beat", project_id, tmp_path, {})

    assert not storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()


def test_beat_stage_skipped_write_due_to_manual_edit_does_not_invalidate_quantize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29回帰: 推論中の手動編集でbeatmap.json自体の書き込みがskipされた場合、

    beatmap.jsonの内容は変わっていないためquantizeを無効化してはいけない。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)
    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})
    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()

    beatmap_path = storage.beatmap_path(tmp_path, project_id)
    (tmp_path / project_id / "source.wav").write_bytes(
        b"RIFF....WAVEfmt X"
    )  # audio変更

    class _FakeResult:
        def __init__(self) -> None:
            self.beats: list = []
            self.downbeats_sec: list = []

        def to_dict(self) -> dict:
            return {
                "beats": [],
                "downbeats_sec": [],
                "time_signatures": [],
                "tempo_map": [],
                "confidence": 0.0,
            }

    def _run_and_concurrently_patch(_audio_path: str) -> _FakeResult:
        manual_beatmap = {
            "beats": [],
            "downbeats_sec": [9.99],
            "time_signatures": [],
            "tempo_map": [],
            "confidence": 0.0,
            "source": "manual",
        }
        storage.write_json(beatmap_path, manual_beatmap)
        return _FakeResult()

    monkeypatch.setattr(dsp_main, "run_beat_estimation", _run_and_concurrently_patch)
    dsp_main.run_beat_stage("job_beat", project_id, tmp_path, {})

    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()


def test_transcribe_stage_write_invalidates_quantize_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29: score/current.jsonへ実際に新しい採譜結果を書いた場合、quantizeのmeta.jsonを無効化する。"""
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)
    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})
    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()

    # 音源(piano stem)を変えて再採譜させる(スキップさせない)。
    (tmp_path / project_id / "stems" / "piano.wav").write_bytes(b"RIFF....WAVEfmt X")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 62, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr2", project_id, tmp_path, {})

    assert not storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()


def test_transcribe_stage_skipped_write_concurrent_modification_does_not_invalidate_quantize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29回帰: 推論中の並行変更でscore/current.jsonの書き込みがskipされた場合、

    採譜結果の内容は反映されていないためquantizeを無効化してはいけない。
    """
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr1", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)
    dsp_main.run_quantize_stage("job_q", project_id, tmp_path, {})
    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()

    # 音源を変えて再採譜対象にしつつ、推論中に別プロセスがScore IRを書き換えたと模擬する。
    (tmp_path / project_id / "stems" / "piano.wav").write_bytes(b"RIFF....WAVEfmt X")

    def _transcribe_and_concurrently_modify(audio_path: Path):
        service = ScoreService(workspace_dir=tmp_path)
        score = service.read_score(project_id)
        score.meta.stages["_concurrent_marker"] = {"touched": True}
        service.write_score(project_id, score)
        return _fake_transcription_result([(0.0, 0.5, 62, 90, False)])(audio_path)

    monkeypatch.setattr(
        dsp_main, "run_piano_transcription", _transcribe_and_concurrently_modify
    )
    dsp_main.run_transcribe_stage("job_tr2", project_id, tmp_path, {})

    assert storage.stage_metadata_path(tmp_path, project_id, "quantize").exists()


def test_quantize_stage_force_bypasses_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29: `force=True`はquantizeでもハッシュ一致にかかわらず再実行させる。"""
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        _fake_transcription_result([(0.0, 0.5, 60, 90, False)]),
    )
    dsp_main.run_transcribe_stage("job_tr", project_id, tmp_path, {})
    _write_beatmap(tmp_path, project_id)

    from app.pipeline.quantize import quantize_note_onsets as _original_quantize

    call_count = 0

    def _counting_quantize(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _original_quantize(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "quantize_note_onsets", _counting_quantize)
    dsp_main.run_quantize_stage("job_q1", project_id, tmp_path, {})
    assert call_count == 1
    dsp_main.run_quantize_stage("job_q2", project_id, tmp_path, {"force": True})
    assert call_count == 2  # forceによりハッシュ一致でも再実行された


def test_force_rerun_with_failed_invalidation_resets_own_metadata_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#29-M2レビュー指摘の回帰: `force=True`かつ入力/パラメータ不変のまま再実行した際、

    `invalidate_downstream`がリトライ上限超過等で失敗しても、自ステージの
    meta.jsonを削除しておくことで、次回(非force)実行時に必ず再実行され
    無効化が再試行される(自ステージのmetaが古いハッシュのまま残ると、
    次回`should_skip_stage`でスキップされ無効化が二度と呼ばれなくなる
    問題を防ぐ、`_invalidate_downstream_or_reset`)。
    """
    project_id = "proj_test"
    _run_full_pipeline_once(tmp_path, monkeypatch, project_id)
    assert storage.stage_metadata_path(tmp_path, project_id, "separate").exists()
    assert storage.stage_metadata_path(tmp_path, project_id, "transcribe").exists()

    monkeypatch.setattr(dsp_main, "run_separation", _fake_run_separation(["piano"]))

    def _failing_invalidate(*args, **kwargs):
        raise PermissionError("simulated retry-exhausted failure")

    monkeypatch.setattr(
        dsp_main.stage_invalidation, "invalidate_downstream", _failing_invalidate
    )
    with pytest.raises(PermissionError):
        dsp_main.run_separate_stage(
            "job_sep_force",
            project_id,
            tmp_path,
            {"preset": "standard", "execution_provider": "cpu", "force": True},
        )
    # 無効化失敗により自ステージのmetaは削除され、下流(transcribe)のmetaは
    # (無効化されないまま)古い状態で残っている。
    assert not storage.stage_metadata_path(tmp_path, project_id, "separate").exists()
    assert storage.stage_metadata_path(tmp_path, project_id, "transcribe").exists()

    # 無効化を復旧させ、forceを付けずに再実行する: 自ステージのmetaが
    # 削除されているため(ハッシュ一致でも)スキップされず、無効化も
    # 再試行されてtranscribeのmetaが無効化されるべき。
    monkeypatch.undo()
    call_count = 0
    real_run = _fake_run_separation(["piano"])

    def _counting_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(dsp_main, "run_separation", _counting_run)
    dsp_main.run_separate_stage(
        "job_sep_retry",
        project_id,
        tmp_path,
        {"preset": "standard", "execution_provider": "cpu"},
    )
    assert call_count == 1  # metaが削除されていたためスキップされず再実行された
    assert not storage.stage_metadata_path(tmp_path, project_id, "transcribe").exists()


def test_transcribe_stage_with_bass_stem_creates_bass_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#54: bass.wav がある場合、Score IR に Bass パート(staves=1, clefs=[F4], midi_program=33)が生成されること。"""
    from app.pipeline.transcribe.piano import NoteEvent, TranscriptionResult

    project_id = "proj_bass"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")

    fake_bass_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=40, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=0.5, duration_sec=0.5, midi=43, velocity=85, ghost_candidate=False
        ),
    ]

    monkeypatch.setattr(
        dsp_main,
        "run_bass_transcription",
        lambda _p, **_k: TranscriptionResult(notes=fake_bass_notes, pedals=[]),
    )

    dsp_main.run_transcribe_stage("job_bass", project_id, tmp_path, {})

    score = ScoreService(workspace_dir=tmp_path).read_score_optional(project_id)
    assert score is not None
    bass_part = score.find_part("bass")
    assert bass_part is not None
    assert bass_part.name == "Bass"
    assert bass_part.midi_program == 33
    assert bass_part.staves == 1
    assert len(bass_part.clefs) == 1
    assert bass_part.clefs[0].sign == "F"
    assert bass_part.clefs[0].line == 4
    assert len(bass_part.notes) == 2
    assert bass_part.notes[0].midi == 40
    assert bass_part.notes[1].midi == 43


def test_transcribe_stage_with_both_piano_and_bass_stems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#54: piano.wav と bass.wav の両方がある場合、両パートが Score IR に生成されること。"""
    from app.pipeline.transcribe.piano import NoteEvent, TranscriptionResult

    project_id = "proj_both"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")

    monkeypatch.setattr(
        dsp_main,
        "run_piano_transcription",
        lambda _p, **_k: TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=60,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        ),
    )
    monkeypatch.setattr(
        dsp_main,
        "run_bass_transcription",
        lambda _p, **_k: TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=36,
                    velocity=80,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        ),
    )

    dsp_main.run_transcribe_stage("job_both", project_id, tmp_path, {})

    score = ScoreService(workspace_dir=tmp_path).read_score_optional(project_id)
    assert score is not None
    piano_part = score.find_part("piano")
    bass_part = score.find_part("bass")
    assert piano_part is not None
    assert bass_part is not None
    assert len(piano_part.notes) == 1
    assert len(bass_part.notes) == 1


def test_transcribe_stage_bass_octave_shift_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#54 / R-6: 生成された Bass パートに対して PartTransposeOctaveOp(part_id="bass") が適用できること。"""
    from app.domain.score_ops import PartTransposeOctaveOp
    from app.pipeline.transcribe.piano import NoteEvent, TranscriptionResult
    from app.services import score_ops

    project_id = "proj_bass_shift"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")

    fake_bass_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=40, velocity=80, ghost_candidate=False
        ),
    ]
    monkeypatch.setattr(
        dsp_main,
        "run_bass_transcription",
        lambda _p, **_k: TranscriptionResult(notes=fake_bass_notes, pedals=[]),
    )

    dsp_main.run_transcribe_stage("job_shift", project_id, tmp_path, {})

    score_service = ScoreService(workspace_dir=tmp_path)
    score = score_service.read_score(project_id)
    bass_part = score.find_part("bass")
    assert bass_part is not None
    assert bass_part.notes[0].midi == 40

    # 1オクターブ上にシフト
    score_ops.apply_ops(
        score,
        [PartTransposeOctaveOp(part_id="bass", direction="up")],
        beat_anchors=[],
    )
    bass_part = score.find_part("bass")
    assert bass_part is not None
    assert bass_part.notes[0].midi == 52

    # 1オクターブ下にシフト
    score_ops.apply_ops(
        score,
        [PartTransposeOctaveOp(part_id="bass", direction="down")],
        beat_anchors=[],
    )
    bass_part = score.find_part("bass")
    assert bass_part is not None
    assert bass_part.notes[0].midi == 40


def test_transcribe_stage_skip_rejected_when_one_part_notes_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#124 レビュー指摘: 両ステムが存在する状態で片方のノートだけ削除された場合、スキップが拒否されて再採譜されること。"""
    from app.pipeline.transcribe.piano import NoteEvent, TranscriptionResult

    project_id = "proj_both_delete"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")

    call_counts = {"piano": 0, "bass": 0}

    def _mock_piano(_p, **_k):
        call_counts["piano"] += 1
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=60,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    def _mock_bass(_p, **_k):
        call_counts["bass"] += 1
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=36,
                    velocity=80,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _mock_piano)
    monkeypatch.setattr(dsp_main, "run_bass_transcription", _mock_bass)

    # 初回実行
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    assert call_counts == {"piano": 1, "bass": 1}

    # 入力不変ならスキップされること
    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})
    assert call_counts == {"piano": 1, "bass": 1}

    # ピアノパートのノートだけを削除した状態を作る
    score_service = ScoreService(workspace_dir=tmp_path)
    score = score_service.read_score(project_id)
    piano_part = score.find_part("piano")
    assert piano_part is not None
    piano_part.notes = []
    score_service.write_score(project_id, score)

    # 再実行: ピアノノートが欠落しているためスキップが拒否され再採譜されること
    dsp_main.run_transcribe_stage("job3", project_id, tmp_path, {})
    assert call_counts == {"piano": 2, "bass": 2}


def test_transcribe_stage_records_bass_algo_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#124 レビュー指摘: bass_algo_version が provider_versions およびハッシュに反映されること。"""
    from app.pipeline.transcribe.bass import BASS_ALGO_VERSION
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_bass_ver"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")

    def _mock_bass(_p, **_k):
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=36,
                    velocity=80,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_bass_transcription", _mock_bass)

    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})

    meta_path = storage.stage_metadata_path(tmp_path, project_id, "transcribe")
    assert meta_path.exists()
    meta = storage.read_json(meta_path)
    assert meta["versions"].get("bass_transcription") == BASS_ALGO_VERSION
    assert "librosa" in meta["versions"]
    assert "scipy" in meta["versions"]


def test_transcribe_stage_with_vocals_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vocals.wav 単独存在時の Stage 3 採譜テスト(#55)。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult
    from app.pipeline.transcribe.vocals import VOCALS_ALGO_VERSION

    project_id = "proj_vocals_only"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "vocals.wav")

    def _mock_vocals(_p, **_k):
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.5,
                    duration_sec=1.0,
                    midi=69,
                    velocity=85,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_vocals_transcription", _mock_vocals)

    dsp_main.run_transcribe_stage("job_v1", project_id, tmp_path, {})

    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    assert score.find_part("piano") is None
    assert score.find_part("bass") is None

    vocals_part = score.find_part("vocals")
    assert vocals_part is not None
    assert vocals_part.name == "Vocals"
    assert vocals_part.midi_program == 53
    assert vocals_part.staves == 1
    assert vocals_part.clefs[0].sign == "G"
    assert vocals_part.clefs[0].line == 2
    assert len(vocals_part.notes) == 1
    assert vocals_part.notes[0].midi == 69

    # メタデータ記録確認
    meta_path = storage.stage_metadata_path(tmp_path, project_id, "transcribe")
    meta = storage.read_json(meta_path)
    assert meta["versions"].get("vocals_transcription") == VOCALS_ALGO_VERSION
    assert "torchcrepe" in meta["versions"]


def test_transcribe_stage_with_piano_bass_vocals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Piano + Bass + Vocals の 3 ステム同時採譜およびサマリーメッセージの検証(#55)。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_three_stems"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "vocals.wav")

    def _mock_piano(_p, **_k):
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=60,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    def _mock_bass(_p, **_k):
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=36,
                    velocity=80,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    def _mock_vocals(_p, **_k):
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=69,
                    velocity=85,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _mock_piano)
    monkeypatch.setattr(dsp_main, "run_bass_transcription", _mock_bass)
    monkeypatch.setattr(dsp_main, "run_vocals_transcription", _mock_vocals)

    emitted = []
    monkeypatch.setattr(dsp_main, "emit", lambda p: emitted.append(p))

    dsp_main.run_transcribe_stage("job_all", project_id, tmp_path, {})

    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    assert score.find_part("piano") is not None
    assert score.find_part("bass") is not None
    assert score.find_part("vocals") is not None

    # メッセージに 3 パートすべての情報が含まれていること
    final_msg = emitted[-1]["message"]
    assert "3 notes (1 piano, 1 bass, 1 vocals)" in final_msg


def test_transcribe_stage_skip_rejected_when_vocals_notes_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 ステム存在時に vocals ノートだけ手動削除された場合、スキップが拒否されること(#55, AND 条件)。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_vocals_del"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "bass.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "vocals.wav")

    call_counts = {"piano": 0, "bass": 0, "vocals": 0}

    def _mock_piano(_p, **_k):
        call_counts["piano"] += 1
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=60,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    def _mock_bass(_p, **_k):
        call_counts["bass"] += 1
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=36,
                    velocity=80,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    def _mock_vocals(_p, **_k):
        call_counts["vocals"] += 1
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=69,
                    velocity=85,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _mock_piano)
    monkeypatch.setattr(dsp_main, "run_bass_transcription", _mock_bass)
    monkeypatch.setattr(dsp_main, "run_vocals_transcription", _mock_vocals)

    # 初回実行
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    assert call_counts == {"piano": 1, "bass": 1, "vocals": 1}

    # 入力不変ならスキップされること
    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})
    assert call_counts == {"piano": 1, "bass": 1, "vocals": 1}

    # vocals のノートだけ削除
    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    vocals_part = score.find_part("vocals")
    assert vocals_part is not None
    vocals_part.notes = []
    service.write_score(project_id, score)

    # 再実行: スキップが拒否されて全パート再採譜されること
    dsp_main.run_transcribe_stage("job3", project_id, tmp_path, {})
    assert call_counts == {"piano": 2, "bass": 2, "vocals": 2}


def test_transcribe_stage_skips_when_stem_originally_had_zero_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無音ステム(採譜結果0件)が存在する場合、2回目実行で正常にスキップされること(#55, #125 レビュー指摘)。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_silent_vocals"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "piano.wav")
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "vocals.wav")

    call_counts = {"piano": 0, "vocals": 0}

    def _mock_piano(_p, **_k):
        call_counts["piano"] += 1
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=60,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    def _mock_silent_vocals(_p, **_k):
        call_counts["vocals"] += 1
        return TranscriptionResult(notes=[], pedals=[])

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _mock_piano)
    monkeypatch.setattr(dsp_main, "run_vocals_transcription", _mock_silent_vocals)

    # 初回実行
    dsp_main.run_transcribe_stage("job1", project_id, tmp_path, {})
    assert call_counts == {"piano": 1, "vocals": 1}

    # stage_metadata に note_counts が記録されていることを確認
    meta_path = storage.stage_metadata_path(tmp_path, project_id, "transcribe")
    assert meta_path.exists()
    meta = storage.read_json(meta_path)
    assert meta.get("extra", {}).get("note_counts") == {"piano": 1, "vocals": 0}

    # 2回目実行: vocals が 0 ノートであっても手動削除ではないため正常にスキップされること
    dsp_main.run_transcribe_stage("job2", project_id, tmp_path, {})
    assert call_counts == {"piano": 1, "vocals": 1}


def test_transcribe_stage_with_guitar_stem_creates_guitar_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#56: guitar.wav がある場合、Score IR に Guitar パート(staves=1, G clef, midi_program=25)が生成されること。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult
    from app.pipeline.transcribe.guitar import GUITAR_ALGO_VERSION

    project_id = "proj_guitar"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "guitar.wav")

    fake_guitar_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=52, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=57, velocity=85, ghost_candidate=False
        ),
    ]

    monkeypatch.setattr(
        dsp_main,
        "run_guitar_transcription",
        lambda _p, **_k: TranscriptionResult(notes=fake_guitar_notes, pedals=[]),
    )

    dsp_main.run_transcribe_stage("job_guitar", project_id, tmp_path, {})

    score = ScoreService(workspace_dir=tmp_path).read_score_optional(project_id)
    assert score is not None
    guitar_part = score.find_part("guitar")
    assert guitar_part is not None
    assert guitar_part.name == "Guitar"
    assert guitar_part.midi_program == 25
    assert guitar_part.staves == 1
    assert len(guitar_part.clefs) == 1
    assert guitar_part.clefs[0].sign == "G"
    assert guitar_part.clefs[0].line == 2
    assert len(guitar_part.notes) == 2
    assert {n.midi for n in guitar_part.notes} == {52, 57}
    assert all(n.voice == 1 and n.staff == 1 for n in guitar_part.notes)

    meta_path = storage.stage_metadata_path(tmp_path, project_id, "transcribe")
    meta = storage.read_json(meta_path)
    assert meta["versions"].get("guitar_transcription") == GUITAR_ALGO_VERSION
    assert "onnxruntime" in meta["versions"]


def test_transcribe_stage_with_other_stem_creates_other_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#56: other.wav がある場合、Score IR に Other パート(staves=1, G clef)が生成されること。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_other"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_stub_wav(storage.stems_dir(tmp_path, project_id) / "other.wav")

    fake_other_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.5, midi=64, velocity=75, ghost_candidate=False
        ),
    ]

    monkeypatch.setattr(
        dsp_main,
        "run_guitar_transcription",
        lambda _p, **_k: TranscriptionResult(notes=fake_other_notes, pedals=[]),
    )

    dsp_main.run_transcribe_stage("job_other", project_id, tmp_path, {})

    score = ScoreService(workspace_dir=tmp_path).read_score_optional(project_id)
    assert score is not None
    other_part = score.find_part("other")
    assert other_part is not None
    assert other_part.name == "Other"
    assert other_part.staves == 1
    assert len(other_part.notes) == 1
    assert other_part.notes[0].midi == 64


def test_transcribe_stage_with_all_five_stems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Piano + Bass + Vocals + Guitar + Other の5ステム同時採譜およびサマリーメッセージの検証(#56)。"""
    from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult

    project_id = "proj_five_stems"
    _setup_project_with_valid_source(tmp_path, project_id)
    for stem in ["piano", "bass", "vocals", "guitar", "other"]:
        _write_stub_wav(storage.stems_dir(tmp_path, project_id) / f"{stem}.wav")

    def _single_note(midi: int):
        def _mock(_p, **_k):
            return TranscriptionResult(
                notes=[
                    NoteEvent(
                        onset_sec=0.0,
                        duration_sec=0.5,
                        midi=midi,
                        velocity=70,
                        ghost_candidate=False,
                    )
                ],
                pedals=[],
            )

        return _mock

    monkeypatch.setattr(dsp_main, "run_piano_transcription", _single_note(60))
    monkeypatch.setattr(dsp_main, "run_bass_transcription", _single_note(36))
    monkeypatch.setattr(dsp_main, "run_vocals_transcription", _single_note(69))

    # guitar/otherは同じ`run_guitar_transcription`関数を共有するため、渡された
    # ステムパスから呼び出し元を判別してノートを出し分ける(#56)。
    def _mock_guitar_or_other(stem_path: Path, **_k):
        midi = 52 if "guitar" in stem_path.name else 64
        return TranscriptionResult(
            notes=[
                NoteEvent(
                    onset_sec=0.0,
                    duration_sec=0.5,
                    midi=midi,
                    velocity=70,
                    ghost_candidate=False,
                )
            ],
            pedals=[],
        )

    monkeypatch.setattr(dsp_main, "run_guitar_transcription", _mock_guitar_or_other)

    emitted = []
    monkeypatch.setattr(dsp_main, "emit", lambda p: emitted.append(p))

    dsp_main.run_transcribe_stage("job_all5", project_id, tmp_path, {})

    service = ScoreService(workspace_dir=tmp_path)
    score = service.read_score(project_id)
    guitar_part = score.find_part("guitar")
    other_part = score.find_part("other")
    assert score.find_part("piano") is not None
    assert score.find_part("bass") is not None
    assert score.find_part("vocals") is not None
    assert guitar_part is not None
    assert other_part is not None
    assert guitar_part.notes[0].midi == 52
    assert other_part.notes[0].midi == 64

    final_msg = emitted[-1]["message"]
    assert "5 notes (1 piano, 1 bass, 1 vocals, 1 guitar, 1 other)" in final_msg
