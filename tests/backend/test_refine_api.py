"""#39/#104: L1整音API(`POST /api/projects/{id}/refine`)のテスト。

`run_l1_sequential`自体のロジックは`test_l1_runner.py`で検証済み。ここでは
API層の配線(404/503/422/200、`current.json`が変更されないこと、
`score/staging/{run_id}.json`への保存)のみを検証する。実際の`claude` CLI
呼び出しは`app.pipeline.refine.l1_runner.call_l1_chunk`をモックして防ぐ
(#104: CIランナーには`claude` CLIが無い前提)。
"""

from __future__ import annotations

from unittest.mock import patch

from app.config import Settings
from app.domain.score import Clef, Note, Part, ScoreIR, SourceInfo
from app.infra import storage
from app.pipeline.refine.l1_client import L1ChunkCallResult, L1ChunkResponse
from app.services.score_service import ScoreService
from fastapi.testclient import TestClient


def _create_project(client: TestClient, tiny_wav_bytes: bytes) -> str:
    resp = client.post(
        "/api/projects", files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _write_score(settings: Settings, project_id: str) -> None:
    service = ScoreService(workspace_dir=settings.workspace_dir)
    score = ScoreIR(
        project_id=project_id,
        source=SourceInfo(filename="song.wav", duration_sec=2.0, sample_rate=8000),
        divisions=480,
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2), Clef(staff=2, sign="F", line=4)],
    )
    part.notes.append(
        Note(
            id=score.allocate_note_id(),
            onset_sec=0.0,
            duration_sec=0.5,
            onset_tick=0,
            duration_tick=480,
            midi=60,
            velocity=90,
            provenance="amt",
            voice=1,
            staff=1,
        )
    )
    score.parts.append(part)
    service.write_score(project_id, score)


def _write_beatmap_120bpm_4_4(settings: Settings, project_id: str) -> None:
    beats = [
        {"time_sec": i * 0.5, "beat_in_bar": (i % 4) + 1, "bar": (i // 4) + 1}
        for i in range(16)
    ]
    storage.write_json(
        storage.beatmap_path(settings.workspace_dir, project_id),
        {
            "beats": beats,
            "downbeats_sec": [b["time_sec"] for b in beats if b["beat_in_bar"] == 1],
            "time_signatures": [{"bar": 1, "numerator": 4, "denominator": 4}],
            "tempo_map": [],
            "confidence": 1.0,
        },
    )


def _mock_l1_response():
    result = L1ChunkCallResult(
        output=L1ChunkResponse(bar_range=[1, 1], decisions=[]),
        usage={
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        cost_usd=0.002,
    )
    return patch("app.pipeline.refine.l1_runner.call_l1_chunk", return_value=result)


def _cli_available(available: bool):
    """`api/refine.py`の`is_claude_cli_available`ゲートをモックする(#104)。

    実際のPATH上に`claude`があるかどうかに関わらず、テストは常にこの結果を
    使う(CIランナーには`claude` CLIが無い前提のため)。
    """
    return patch("app.api.refine.is_claude_cli_available", return_value=available)


def test_refine_404_before_transcribe(
    client: TestClient, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "sync", "part_id": "piano"},
    )
    assert resp.status_code == 404


def test_refine_503_when_claude_cli_unavailable(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    with _cli_available(False):
        resp = client.post(
            f"/api/projects/{project_id}/refine",
            json={"mode": "sync", "part_id": "piano"},
        )
    assert resp.status_code == 503, resp.text


def test_refine_404_when_beatmap_missing(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    # beatmap.jsonを書かない

    with _cli_available(True):
        resp = client.post(
            f"/api/projects/{project_id}/refine",
            json={"mode": "sync", "part_id": "piano"},
        )
    assert resp.status_code == 404, resp.text


def test_refine_success_writes_staging_and_does_not_touch_current(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    current_path = storage.score_current_path(settings.workspace_dir, project_id)
    before = current_path.read_bytes()

    with _cli_available(True), _mock_l1_response():
        resp = client.post(
            f"/api/projects/{project_id}/refine",
            json={"mode": "sync", "part_id": "piano"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks_ok"] == 1
    assert body["chunks_rejected"] == 0
    assert "cost_usd" in body
    assert body["cost_usd"] >= 0.0

    # current.jsonは一切変更されない(#39の設計判断)。
    assert current_path.read_bytes() == before

    staging_path = storage.score_staging_path(
        settings.workspace_dir, project_id, body["run_id"]
    )
    assert staging_path.exists()


def test_refine_rejects_unknown_part_id(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    with _cli_available(True):
        resp = client.post(
            f"/api/projects/{project_id}/refine",
            json={"mode": "sync", "part_id": "nonexistent"},
        )
    assert resp.status_code == 422, resp.text


def test_refine_batch_mode_success(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#40/#104: mode: 'batch' で並列CLI呼び出しランナーが呼び出され、cost_usdが返る。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)
    _write_beatmap_120bpm_4_4(settings, project_id)

    service = ScoreService(workspace_dir=settings.workspace_dir)
    staged_score = service.read_score(project_id)

    from app.pipeline.refine.l1_runner import L1RunResult

    mock_run_result = L1RunResult(
        staged_score=staged_score,
        chunks_ok=1,
        chunks_rejected=0,
        # #109: 検証層の機械的修復(NFR-06でUIへ表示する経路)。
        voice_repairs=["bars (1, 4) note 7: voice 1 -> 2"],
        usage={
            "input_tokens": 2000,
            "output_tokens": 1000,
            "cache_read_input_tokens": 0,
        },
        cost_usd=0.5,
    )

    with (
        _cli_available(True),
        patch(
            "app.api.refine.run_l1_batch", return_value=mock_run_result
        ) as mock_batch,
    ):
        resp = client.post(
            f"/api/projects/{project_id}/refine",
            json={"mode": "batch", "part_id": "piano"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks_ok"] == 1
    assert body["chunks_rejected"] == 0
    assert body["cost_usd"] == 0.5
    assert body["voice_repairs"] == ["bars (1, 4) note 7: voice 1 -> 2"]
    mock_batch.assert_called_once()


def test_refine_estimate_default_mode_is_sync(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#40: GET /refine/estimate は未指定時に mode='sync' を既定とする。"""
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)

    resp = client.get(
        f"/api/projects/{project_id}/refine/estimate",
        params={"part_id": "piano"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["part_id"] == "piano"
    assert body["mode"] == "sync"
    assert body["num_chunks"] >= 1
    assert body["estimated_input_tokens"] > 0
    assert body["estimated_output_tokens"] > 0
    assert body["estimated_cost_usd"] > 0


def test_refine_estimate_batch_mode_costs_more_than_sync(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    """#104: batchはAnthropic Batches APIの割引ではなく並列CLI実行に再定義され、

    割引は伴わない。さらに並列実行はプロンプトキャッシュの再利用がほぼ効かない
    ため、見積もりコストはsync以上になる(チャンクが2件以上ある場合は厳密に
    高くなる。この計算式自体の詳細は`test_l1_cost.py`で単体検証済み。ここでは
    APIの配線のみを検証するため、このfixtureのノート数(=チャンク1件)では
    差が出ないことを許容し`>=`で確認する、#104 Gate2レビュー指摘)。
    """
    project_id = _create_project(client, tiny_wav_bytes)
    _write_score(settings, project_id)

    sync_resp = client.get(
        f"/api/projects/{project_id}/refine/estimate",
        params={"part_id": "piano", "mode": "sync"},
    )
    batch_resp = client.get(
        f"/api/projects/{project_id}/refine/estimate",
        params={"part_id": "piano", "mode": "batch"},
    )
    assert sync_resp.status_code == 200, sync_resp.text
    assert batch_resp.status_code == 200, batch_resp.text

    sync_body = sync_resp.json()
    batch_body = batch_resp.json()
    assert batch_body["part_id"] == "piano"
    assert batch_body["mode"] == "batch"
    assert batch_body["num_chunks"] >= 1
    assert batch_body["estimated_cost_usd"] >= sync_body["estimated_cost_usd"]


def test_refine_rejects_invalid_mode(
    client: TestClient, settings: Settings, tiny_wav_bytes: bytes
) -> None:
    project_id = _create_project(client, tiny_wav_bytes)
    resp = client.post(
        f"/api/projects/{project_id}/refine",
        json={"mode": "unsupported_mode", "part_id": "piano"},
    )
    assert resp.status_code == 422
