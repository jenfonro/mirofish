"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..errors import RelayError
from . import admin, openai_compat, relay, webui
from .state import AppState


def create_app(state: AppState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await state.aclose()

    app = FastAPI(title="Mirofish Relay", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.relay = state

    @app.exception_handler(RelayError)
    async def relay_error_handler(_: Request, exc: RelayError) -> JSONResponse:
        return JSONResponse(exc.payload(), status_code=exc.status)

    app.include_router(webui.router)
    app.include_router(admin.router)
    app.include_router(relay.router)
    app.include_router(openai_compat.router)
    assets = webui.STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    return app


__all__ = ["AppState", "create_app"]
