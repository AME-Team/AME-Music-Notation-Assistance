"""FastAPI エントリ(#11)。

NFR-10 / NFR-10′: 127.0.0.1 のみにバインドし、`/health` を除く全 API で
`X-AME-Token` ヘッダを検証する(Electron main が起動時に生成し環境変数で渡す)。
"""

from __future__ import annotations

import argparse
import math

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import jobs, media, projects
from app.config import Settings, load_settings
from app.services.job_service import JobManager
from app.services.project_service import ProjectService

# #78: ブラウザ単独起動(Vite dev server)でのフォールバック開発を許可する。
_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

PUBLIC_PATHS = {"/health"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="AME Music Notation Assistance API", version="0.1.0")

    app.state.settings = settings
    app.state.project_service = ProjectService(workspace_dir=settings.workspace_dir)
    app.state.job_manager = JobManager(workspace_dir=settings.workspace_dir)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def verify_local_token(request: Request, call_next):  # noqa: ANN001
        if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        expected = request.app.state.settings.auth_token
        if expected is not None and request.headers.get("x-ame-token") != expected:
            return JSONResponse(status_code=401, content={"detail": "invalid or missing token"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """NaN/Infinity 拒否バリデーション(#20-M1レビュー指摘の追加ラウンド)を追加すると、

        FastAPI の既定の422レスポンスは `errors()[]["input"]` に実際に送られてきた
        NaN/Infinity値をそのまま含める。Starlette の `JSONResponse` は
        `allow_nan=False` でシリアライズするため、そのNaN/Infinity値自体がレスポンス
        シリアライズで例外を起こし、意図した422ではなく素の500になってしまう。
        JSON非互換な値は文字列表現に置き換えてから返す。
        """
        errors = []
        for error in exc.errors():
            error = dict(error)
            value = error.get("input")
            if isinstance(value, float) and not math.isfinite(value):
                error["input"] = str(value)
            errors.append(error)
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    app.include_router(projects.router)
    app.include_router(jobs.router)
    app.include_router(media.router)

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    settings = load_settings(port=args.port)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
