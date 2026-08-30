"""プロバイダ設定・APIキー解決・起動パラメータ(#11, #79)。

NFR-10 / NFR-10′: APIキーは環境変数または OS の資格情報ストアから読み、
プロジェクトファイルには保存しない。ローカル認証トークンは Electron main が
環境変数経由で渡す(このプロセスが直接生成することはない)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# NFR-08′ / #79: Windows 専用。パス長対策のため ID は短い固定長にする(infra/ids.py参照)。
DEFAULT_WORKSPACE_DIRNAME = "workspace"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    auth_token: str | None
    workspace_dir: Path

    @property
    def db_path(self) -> Path:
        return self.workspace_dir / "db.sqlite3"


def _resolve_workspace_dir() -> Path:
    override = os.environ.get("AME_WORKSPACE_DIR")
    if override:
        return Path(override)
    # リポジトリ直下の workspace/(#12, gitignore対象)。
    return Path(__file__).resolve().parents[2] / DEFAULT_WORKSPACE_DIRNAME


def load_settings(*, port: int = 0) -> Settings:
    """#77: Electron main が起動時に空きポートとトークンを環境変数で渡す。

    ポートが指定されない場合は 0(OS が空きポートを自動割当)を uvicorn に渡す。
    """
    env_port = os.environ.get("AME_BACKEND_PORT")
    resolved_port = int(env_port) if env_port else port
    return Settings(
        host="127.0.0.1",  # NFR-10: 127.0.0.1 のみにバインド
        port=resolved_port,
        auth_token=os.environ.get("AME_BACKEND_TOKEN"),
        workspace_dir=_resolve_workspace_dir(),
    )


def resolve_api_key(provider: str) -> str | None:
    """NFR-10: 環境変数を優先し、無ければ Windows 資格情報マネージャー(keyring)を試す。

    Linux 開発環境など keyring バックエンドが使えない場合は例外を握って None を返す
    (#79: 動作を壊さないフォールバック)。
    """
    env_key = os.environ.get(f"AME_{provider.upper()}_API_KEY")
    if env_key:
        return env_key

    try:
        import keyring

        return keyring.get_password("AME-Music-Notation-Assistance", provider)
    except Exception:
        return None
