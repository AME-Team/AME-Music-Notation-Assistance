from __future__ import annotations

import sys
from pathlib import Path

# `app` パッケージは backend/ 直下にある。pytest の起動方法(uv run / IDE / CI)に依らず
# 確実に import できるよう、ini の pythonpath 設定に頼らずここで明示的に追加する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TEST_TOKEN = "test-token-abc123"


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def settings(workspace_dir: Path) -> Settings:
    return Settings(
        host="127.0.0.1", port=0, auth_token=TEST_TOKEN, workspace_dir=workspace_dir
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app, headers={"X-AME-Token": TEST_TOKEN})


@pytest_asyncio.fixture
async def async_client(settings: Settings):
    """バックグラウンドタスク(ジョブ実行)がSSE購読と同じイベントループで進行するよう、
    同期版 TestClient ではなく httpx.AsyncClient を単一の実行中ループ内で使う。
    """
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-AME-Token": TEST_TOKEN}
    ) as ac:
        yield ac


@pytest.fixture
def tiny_wav_bytes() -> bytes:
    """44バイトのヘッダのみの極小WAV(pytestで音声ファイルとして扱えれば十分)。"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()
