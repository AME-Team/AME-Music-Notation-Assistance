from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_requires_no_token(settings: Settings) -> None:
    app = create_app(settings)
    client = TestClient(app)  # no token header
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_missing_token_is_rejected(settings: Settings) -> None:
    app = create_app(settings)
    client = TestClient(app)  # no token header
    resp = client.get("/api/projects")
    assert resp.status_code == 401


def test_wrong_token_is_rejected(settings: Settings) -> None:
    app = create_app(settings)
    client = TestClient(app, headers={"X-AME-Token": "wrong"})
    resp = client.get("/api/projects")
    assert resp.status_code == 401


def test_project_lifecycle(client: TestClient, tiny_wav_bytes: bytes) -> None:
    # create
    resp = client.post(
        "/api/projects",
        files={"file": ("song.wav", tiny_wav_bytes, "audio/wav")},
    )
    assert resp.status_code == 201, resp.text
    project = resp.json()
    assert project["audio_format"] == "wav"
    assert project["original_filename"] == "song.wav"
    project_id = project["id"]

    # list
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert any(p["id"] == project_id for p in resp.json()["projects"])

    # get
    resp = client.get(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == project_id

    # play (range request)
    resp = client.get(f"/api/projects/{project_id}/audio/original", headers={"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.headers["content-range"].startswith("bytes 0-9/")
    assert len(resp.content) == 10

    # delete
    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 204

    resp = client.get(f"/api/projects/{project_id}")
    assert resp.status_code == 404


def test_unsupported_format_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/projects",
        files={"file": ("song.xyz", b"not audio", "application/octet-stream")},
    )
    assert resp.status_code == 422
