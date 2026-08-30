"""OpenAPI スキーマのエクスポート(#14, NFR-09)。

手書きの型二重管理を禁止するため、Pydantic モデルから生成される OpenAPI スキーマを
そのまま `backend/openapi.json` に書き出す。フロントはこれを `openapi-typescript` で
TS 型に変換する(`frontend/package.json` の `typegen` スクリプト)。

サーバを実際に起動する必要はない(FastAPI app インスタンスから直接生成する)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


def main() -> None:
    app = create_app()
    schema = app.openapi()
    out_path = Path(__file__).resolve().parents[1] / "openapi.json"
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
