"""#21: メディア・解析APIのテスト。ステム/ピーク/beatmapの配信とBeatGridEditorの補正。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.infra import storage


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_list_stems_returns_empty_before_separation(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    """#21 TrackList: 分離ステージ未実行時は空リストを200で返す(エラーではない)。"""
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/stems")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"names": []}


def test_list_stems_returns_sorted_names_after_separation(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    stems_dir = storage.stems_dir(settings.workspace_dir, project_id)
    stems_dir.mkdir(parents=True, exist_ok=True)
    for name in ("vocals", "drums", "bass"):
        (stems_dir / f"{name}.wav").write_bytes(tiny_wav_bytes)

    resp = client.get(f"/api/projects/{project_id}/stems")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"names": ["bass", "drums", "vocals"]}


def test_list_stems_404_for_missing_project(client: TestClient) -> None:
    resp = client.get("/api/projects/nonexistent/stems")
    assert resp.status_code == 404


def test_peaks_endpoint_computes_and_caches(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/original")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sample_rate"] == 8000  # conftest.tiny_wav_bytes は 8kHz
    assert len(body["peaks"]) > 0

    cache_path = storage.peaks_path(settings.workspace_dir, project_id, "original")
    assert cache_path.exists()


def test_peaks_endpoint_returns_415_for_undecodable_audio(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): libsndfileがデコードできない

    フォーマット(例: m4a/AAC)の原曲に対しては、未処理の500ではなく415
    (Unsupported Media Type)を返すべき(get_original_audioは原曲として
    mp3/wav/flac/m4aを受け付ける)。422はFastAPIのリクエスト検証エラーの
    既定ステータスであり、音源データ自体が処理できないことを表すには
    不適切なため415を採用する。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    # 原曲ファイルを、libsndfileがデコードできない中身(WAVヘッダを装わない
    # 生バイト列)に差し替える。conftestのtiny_wav_bytesは実際にはWAVとして
    # 有効なため、拡張子はwavのままだが中身を壊すことでデコード失敗を模擬する。
    audio_path = storage.find_original_audio(settings.workspace_dir, project_id)
    audio_path.write_bytes(b"not a real audio file")

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/original")
    assert resp.status_code == 415, resp.text

    cache_path = storage.peaks_path(settings.workspace_dir, project_id, "original")
    assert not cache_path.exists()  # デコード失敗時はキャッシュを作らない


def test_peaks_endpoint_returns_404_if_file_deleted_during_decode(
    client: TestClient,
    settings: Settings,
    tiny_wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): 存在チェックとcompute_peaks呼び出しの

    間に(パイプライン外で)ファイルが削除されるレースが起きた場合、libsndfileの
    システムエラーもLibsndfileErrorとして捕捉されうる。これを422(デコード不能)
    ではなく、本PRが確立した「欠損時は一貫して404」という契約に合わせて404に
    するべき。
    """
    import app.api.media as media_module

    project_id = _create_project(client, tiny_wav_bytes)
    audio_path = storage.find_original_audio(settings.workspace_dir, project_id)

    def _compute_peaks_and_delete(path: str) -> dict:
        # exists()チェック通過後、実際のデコード試行前にファイルが消えたことを模擬する。
        audio_path.unlink()
        import soundfile as sf

        raise sf.LibsndfileError(1, prefix="simulated: file vanished mid-decode: ")

    monkeypatch.setattr(media_module, "compute_peaks", _compute_peaks_and_delete)

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/original")
    assert resp.status_code == 404, resp.text


def test_peaks_endpoint_invalidates_cache_and_404s_if_stem_deleted(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): ピークキャッシュ作成後にステムが

    (パイプライン外で)手動削除された場合、/audio/stems/{name} は404になるのに
    ピークだけ200で返り続けてはいけない。キャッシュ返却前に元音源の存在を
    確認し、欠損していればキャッシュを破棄して404にするべき。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    stems_dir = storage.stems_dir(settings.workspace_dir, project_id)
    stems_dir.mkdir(parents=True, exist_ok=True)
    (stems_dir / "vocals.wav").write_bytes(tiny_wav_bytes)

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/vocals")
    assert resp.status_code == 200, resp.text
    cache_path = storage.peaks_path(settings.workspace_dir, project_id, "vocals")
    assert cache_path.exists()

    (stems_dir / "vocals.wav").unlink()

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/vocals")
    assert resp.status_code == 404, resp.text
    assert not cache_path.exists()  # 欠損検出時にキャッシュも破棄される


def test_peaks_endpoint_invalidates_cache_and_404s_if_original_deleted(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): ステムだけでなく`name == "original"`

    のケース(原曲が手動削除された場合)も同じキャッシュ無効化+404の経路を
    通るべき。
    """
    project_id = _create_project(client, tiny_wav_bytes)

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/original")
    assert resp.status_code == 200, resp.text
    cache_path = storage.peaks_path(settings.workspace_dir, project_id, "original")
    assert cache_path.exists()

    audio_path = storage.find_original_audio(settings.workspace_dir, project_id)
    audio_path.unlink()

    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/original")
    assert resp.status_code == 404, resp.text
    assert not cache_path.exists()

    # 回帰(#21-M1レビュー指摘の追加ラウンド): 原曲を復元すれば、キャッシュが
    # 破棄されているため正常に再計算・再キャッシュされ200に戻るべき
    # (404を返すよう固定化されたままにならない)。
    audio_path.write_bytes(tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/original")
    assert resp.status_code == 200, resp.text
    assert cache_path.exists()


def test_peaks_endpoint_404_for_missing_stem(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/analysis/peaks/vocals")
    assert resp.status_code == 404


def test_stem_audio_404_before_separation(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/audio/stems/vocals")
    assert resp.status_code == 404


def test_stem_audio_content_type_matches_openapi_declaration(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#21-M1レビュー指摘の追加ラウンド): OpenAPIで宣言した `audio/wav` が

    実際のレスポンスヘッダとも一致するべき。`FileResponse` が既定の
    `mimetypes.guess_type()` に頼ると、Linux環境では `.wav` が `audio/x-wav` に
    解決され、宣言と実態が食い違ってしまう。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    stems_dir = storage.stems_dir(settings.workspace_dir, project_id)
    stems_dir.mkdir(parents=True, exist_ok=True)
    (stems_dir / "vocals.wav").write_bytes(tiny_wav_bytes)

    resp = client.get(f"/api/projects/{project_id}/audio/stems/vocals")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"


def test_beatmap_404_before_beat_stage(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.get(f"/api/projects/{project_id}/analysis/beatmap")
    assert resp.status_code == 404


def _write_beatmap(settings: Settings, project_id: str) -> None:
    beatmap = {
        "beats": [
            {"time_sec": 0.0, "beat_in_bar": 1, "bar": 1},
            {"time_sec": 0.5, "beat_in_bar": 2, "bar": 1},
            {"time_sec": 1.0, "beat_in_bar": 3, "bar": 1},
            {"time_sec": 1.5, "beat_in_bar": 4, "bar": 1},
        ],
        "downbeats_sec": [0.0],
        "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
        "tempo_map": [{"bar": 1, "beat": 2.0, "bpm": 120.0}],
        "confidence": 0.9,
        "source": "auto",
    }
    storage.write_json(
        storage.beatmap_path(settings.workspace_dir, project_id), beatmap
    )


def test_get_beatmap_returns_stored_data(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.get(f"/api/projects/{project_id}/analysis/beatmap")
    assert resp.status_code == 200
    assert resp.json()["downbeats_sec"] == [0.0]
    assert resp.json()["source"] == "auto"


def test_patch_beatmap_applies_offset_and_marks_manual(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap", json={"offset_sec": 0.25}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "manual"
    assert body["downbeats_sec"] == [0.25]

    resp = client.get(f"/api/projects/{project_id}/analysis/beatmap")
    assert resp.json()["downbeats_sec"] == [0.25]


def test_patch_beatmap_with_no_fields_does_not_mark_manual(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰: 何も補正を指定しないPATCHで source が "auto" のままであるべき。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(f"/api/projects/{project_id}/analysis/beatmap", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "auto"

    resp = client.get(f"/api/projects/{project_id}/analysis/beatmap")
    assert resp.json()["source"] == "auto"


def test_patch_beatmap_no_op_bpm_override_does_not_mark_manual(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#20レビュー指摘): bpm<=0はapply_bpm_overrideのno-opパス。実際には

    何も変わっていないのでsourceを"manual"に書き換えてはいけない。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap", json={"bpm_override": 0}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "auto"


def test_patch_beatmap_rejects_nan_offset(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#20-M1レビュー指摘の追加ラウンド): offset_sec/bpm_overrideはfloatフィールド

    のため、Pydantic v2の既定ではNaN/Infinityを許容してしまう(int フィールドは既定で
    拒否されるのと非対称)。NaNが通ると beatmap.json に非標準JSON(NaNリテラル)が
    書き込まれ、フロントの JSON.parse を壊しうるため 422 で弾く。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap",
        content=json.dumps({"offset_sec": float("nan")}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


def test_patch_beatmap_rejects_infinite_bpm(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap",
        content=json.dumps({"bpm_override": float("inf")}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


def test_patch_beatmap_conflicts_if_modified_concurrently(
    client: TestClient,
    settings: Settings,
    tiny_wav_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回帰(#20-M1レビュー指摘の追加ラウンド): PATCHのread-modify-writeの間に

    (beatステージ完了などで)beatmap.jsonが外部から書き換えられていた場合、
    それを気づかずに古い内容で上書きする lost-update を防ぐため 409 にする。
    `apply_offset` の呼び出しをフックし、その最中に(=最初のreadの後・最終writeの
    前に)ファイルを書き換えることで、実際のレースウィンドウを再現する
    (test_dsp_main.py の楽観的並行性制御テストと同じ手法)。
    """
    from app.pipeline import beatmap_edit as beatmap_edit_module

    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)
    beatmap_path = storage.beatmap_path(settings.workspace_dir, project_id)

    real_apply_offset = beatmap_edit_module.apply_offset

    def _apply_offset_and_concurrently_modify(beatmap: dict, offset_sec: float) -> dict:
        current = storage.read_json(beatmap_path)
        storage.write_json(beatmap_path, {**current, "downbeats_sec": [99.0]})
        return real_apply_offset(beatmap, offset_sec)

    monkeypatch.setattr(
        beatmap_edit_module, "apply_offset", _apply_offset_and_concurrently_modify
    )

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap", json={"offset_sec": 0.5}
    )
    assert resp.status_code == 409, resp.text

    # 409を返した場合、PATCH自身は書き込みを行わず、割り込んだ内容が生き残る。
    assert storage.read_json(beatmap_path)["downbeats_sec"] == [99.0]


def test_patch_beatmap_zero_offset_does_not_mark_manual(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#20-M1レビュー指摘): offset_sec=0はapply_bpm_overrideのbpm<=0と

    同じno-opパス。実際には何も変わっていないのでsourceを"manual"に
    書き換えてはいけない。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap", json={"offset_sec": 0}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "auto"


def test_patch_beatmap_identical_time_signature_does_not_mark_manual(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#20-M1レビュー指摘の追加ラウンド): 既存と同じ拍子(bar1は既に4/4)を

    再指定しても実質no-opであり、sourceを"manual"に書き換えてはいけない。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap",
        json={"time_signature_override": {"bar": 1, "numerator": 4, "denominator": 4}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "auto"


def test_patch_beatmap_rejects_invalid_time_signature(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """回帰(#20レビュー指摘): 存在しない小節や 0/0 のような拍子は422にする。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_beatmap(settings, project_id)

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap",
        json={"time_signature_override": {"bar": 99, "numerator": 4, "denominator": 4}},
    )
    assert resp.status_code == 422

    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap",
        json={"time_signature_override": {"bar": 1, "numerator": 0, "denominator": 0}},
    )
    assert resp.status_code == 422


def test_patch_beatmap_404_without_existing_beatmap(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.patch(
        f"/api/projects/{project_id}/analysis/beatmap", json={"offset_sec": 0.1}
    )
    assert resp.status_code == 404


@pytest.mark.slow
async def test_beat_stage_end_to_end_via_job(
    async_client: httpx.AsyncClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#18: 実際に beat ステージを実行し、beatmap.json がAPI経由で取得できることを確認する。"""
    resp = await async_client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    project_id = resp.json()["id"]

    resp = await async_client.post(
        f"/api/projects/{project_id}/stages/beat/run", json={"params": {}}
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    events: list[dict] = []
    async with async_client.stream("GET", f"/api/jobs/{job_id}/events") as stream_resp:
        async for line in stream_resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
            if events and events[-1].get("status") in ("succeeded", "failed"):
                break

    assert events[-1]["status"] == "succeeded", events

    resp = await async_client.get(f"/api/projects/{project_id}/analysis/beatmap")
    assert resp.status_code == 200
    assert resp.json()["source"] == "auto"
