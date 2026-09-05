"""#16: DSP Worker のステージオーケストレーション(実モデルはモックして配管だけ検証)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infra import storage
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
