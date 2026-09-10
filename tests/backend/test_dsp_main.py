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
    dsp_main.run_beat_stage("job1", project_id, tmp_path)

    # ユーザーがBeatGridEditorで手動補正したと仮定する。
    beatmap_path = storage.beatmap_path(tmp_path, project_id)
    beatmap = storage.read_json(beatmap_path)
    beatmap["source"] = "manual"
    beatmap["downbeats_sec"] = [1.23]
    storage.write_json(beatmap_path, beatmap)

    # 音源を変えずに再実行 → スキップされ、手動補正が生き残る。
    dsp_main.run_beat_stage("job2", project_id, tmp_path)

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
    dsp_main.run_beat_stage("job1", project_id, tmp_path)
    assert call_count == 1

    storage.beatmap_path(tmp_path, project_id).unlink()

    dsp_main.run_beat_stage("job2", project_id, tmp_path)
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
    dsp_main.run_beat_stage("job1", project_id, tmp_path)

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
    dsp_main.run_beat_stage("job2", project_id, tmp_path)

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
    dsp_main.run_beat_stage("job3", project_id, tmp_path)
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

    with pytest.raises(ValueError, match="piano stem not found"):
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
    project_id = "proj_test"
    _setup_project_with_valid_source(tmp_path, project_id)
    _write_beatmap(tmp_path, project_id)

    service = ScoreService(workspace_dir=tmp_path)
    service.write_score(project_id, dsp_main._initial_score_ir(project_id, tmp_path))

    with pytest.raises(ValueError, match="piano part has no notes"):
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
