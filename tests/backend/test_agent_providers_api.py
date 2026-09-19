from __future__ import annotations

from fastapi.testclient import TestClient


def test_list_providers_includes_dummy(client: TestClient) -> None:
    resp = client.get("/api/agent/providers")
    assert resp.status_code == 200
    providers = resp.json()
    assert {"name": "dummy", "configured": True} in providers


def test_list_providers_includes_opencode(client: TestClient) -> None:
    resp = client.get("/api/agent/providers")
    assert resp.status_code == 200
    providers = resp.json()
    names = [p["name"] for p in providers]
    assert "opencode" in names
