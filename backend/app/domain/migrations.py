"""Score IR の `schema_version` マイグレーション枠組み(#23, R-8)。

設計書R-8: 「Score IRのスキーマ変更で既存プロジェクトが読めなくなる」リスクへの対応。
`schema_version` を持ち、マイグレーション関数を必ず書く方針(§15 R-8)。

現時点(#23実装時)では `CURRENT_SCHEMA_VERSION` より古いバージョンは存在しないため、
`_MIGRATIONS` は空のまま。将来スキーマを変更する際は、この辞書に
`{旧バージョン: 変換関数}` を登録する。変換関数は生のdict(Pydanticモデル化前)を
受け取り、`schema_version` を1つ進めたdictを返すこと。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.score import CURRENT_SCHEMA_VERSION

# {移行元バージョン: (dict) -> 移行先バージョンのdict}。
# 移行先バージョンは移行元+1でなければならない(1段階ずつ適用するため)。
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


class UnknownSchemaVersionError(ValueError):
    """未知の(登録されたマイグレーション経路が無い) `schema_version` を検出した。"""


class FutureSchemaVersionError(ValueError):
    """`schema_version` が現在サポートしている版より新しい(ダウングレード不可)。"""


def migrate_to_current(data: dict[str, Any]) -> dict[str, Any]:
    """生のdictを現行の `CURRENT_SCHEMA_VERSION` まで段階的に移行する。

    `ScoreIR(**data)` でPydanticモデル化する前に呼ぶこと。既に現行バージョンなら
    そのまま返す(恒等変換)。
    """
    if "schema_version" not in data:
        raise UnknownSchemaVersionError("score data is missing 'schema_version'")
    version = data["schema_version"]
    if not isinstance(version, int):
        raise UnknownSchemaVersionError(f"schema_version must be an int, got {version!r}")

    if version > CURRENT_SCHEMA_VERSION:
        raise FutureSchemaVersionError(
            f"schema_version {version} is newer than the version this build supports "
            f"({CURRENT_SCHEMA_VERSION}); refusing to silently downgrade data"
        )

    while version < CURRENT_SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise UnknownSchemaVersionError(
                f"no migration registered from schema_version {version} "
                f"to {version + 1} (current: {CURRENT_SCHEMA_VERSION})"
            )
        data = migration(data)
        new_version = data.get("schema_version")
        if new_version != version + 1:
            raise UnknownSchemaVersionError(
                f"migration from schema_version {version} did not advance schema_version "
                f"by exactly 1 (got {new_version!r})"
            )
        version = new_version

    return data
