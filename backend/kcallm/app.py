"""FastAPI routes: validated input -> calculation/storage -> decimal-string output."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from .calculation import CalculationError
from .demo import DemoConfig, DemoResetter, demo_enabled
from .models import (
    ConfirmRequest,
    Day,
    Entry,
    EntryUpdate,
    ExportData,
    Food,
    FoodInput,
    FoodUpdate,
    Preview,
    PreviewRequest,
    RestorePreviewRequest,
    RestoreRequest,
    Settings,
)
from .storage import DomainError, Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORIGINS = (
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"
)


def hostname(authority: str) -> str:
    """Read one literal Host authority; never trust forwarded hosts or wildcards."""
    authority = authority.strip()
    if not authority or any(character.isspace() for character in authority) or "*" in authority:
        raise ValueError("Expected one exact hostname.")
    parsed = urlsplit(f"//{authority}")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected one exact hostname.")
    _ = parsed.port  # Reject malformed/out-of-range ports as well.
    return parsed.hostname.lower()


def create_app(data_dir: Path | None = None, include_ai: bool = True) -> FastAPI:
    store = Store(data_dir or Path(os.getenv("KCALLM_DATA_DIR", str(PROJECT_ROOT / ".data"))))
    private_dir = Path(os.getenv("KCALLM_PRIVATE_DIR", str(PROJECT_ROOT / ".private")))
    allowed_origins = {
        origin.strip().rstrip("/")
        for origin in os.getenv("KCALLM_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
        if origin.strip()
    }
    allowed_hosts = {"localhost", "127.0.0.1"}
    for origin in allowed_origins:
        allowed_hosts.add(hostname(urlsplit(origin).netloc))
    for host in os.getenv("KCALLM_ALLOWED_HOSTS", "").split(","):
        if host.strip():
            allowed_hosts.add(hostname(host))
    application = FastAPI(title="KcaLLM", version="0.1.0")
    application.state.store = store
    demo_config = DemoConfig.from_env(PROJECT_ROOT) if demo_enabled() else None
    resetter = DemoResetter(store, demo_config) if demo_config else None
    if resetter:
        resetter.reset()

    def refuse_in_demo(action: str) -> None:
        if demo_config:
            raise DomainError("demo_locked", f"{action} is disabled in the public demo.", 403)

    @application.middleware("http")
    async def guard_origin(request: Request, call_next: RequestResponseEndpoint) -> Response:
        host_headers = request.headers.getlist("host")
        try:
            trusted_host = len(host_headers) == 1 and hostname(host_headers[0]) in allowed_hosts
        except ValueError:
            trusted_host = False
        if not trusted_host:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "untrusted_host",
                        "message": "This request hostname is not allowed.",
                    }
                },
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin.rstrip("/") not in allowed_origins
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "untrusted_origin",
                            "message": "This browser origin is not allowed.",
                        }
                    },
                )
        if resetter and request.url.path.startswith("/api/"):
            resetter.maybe_reset()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @application.exception_handler(CalculationError)
    async def calculation_error(_request: Request, exc: CalculationError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"error": {"code": "invalid_portion", "message": str(exc)}}
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo request bodies, which may contain private images or credentials.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    @application.exception_handler(sqlite3.OperationalError)
    async def database_error(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        busy = "locked" in str(exc).lower() or "busy" in str(exc).lower()
        return JSONResponse(
            status_code=503 if busy else 500,
            content={
                "error": {
                    "code": "database_busy" if busy else "database_error",
                    "message": "Storage is busy; retry the same save."
                    if busy
                    else "Storage is unavailable. Your browser draft has not been confirmed.",
                }
            },
        )

    @application.get("/api/health")
    def health() -> dict[str, str]:
        with store.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "backend": "available"}

    @application.get("/api/foods", response_model=list[Food])
    def list_foods(include_archived: bool = False) -> list[Food]:
        return store.foods(include_archived=include_archived)

    @application.post("/api/foods", response_model=Food, status_code=201)
    def add_food(data: FoodInput) -> Food:
        return store.add_food(data)

    @application.put("/api/foods/{food_id}", response_model=Food)
    def update_food(food_id: str, data: FoodUpdate) -> Food:
        return store.update_food(food_id, data)

    @application.delete("/api/foods/{food_id}", status_code=204)
    def archive_food(food_id: str, revision: int = Query(ge=1)) -> Response:
        store.archive_food(food_id, revision)
        return Response(status_code=204)

    @application.post("/api/preview", response_model=Preview)
    def preview(data: PreviewRequest) -> Preview:
        return store.preview(data)

    @application.get("/api/diary", response_model=Day)
    def diary(date: date) -> Day:
        return store.day(date)

    @application.post("/api/diary/confirm", response_model=Day)
    def confirm(data: ConfirmRequest) -> Day:
        return store.confirm(data)

    @application.put("/api/entries/{entry_id}", response_model=Entry)
    def update_entry(entry_id: str, data: EntryUpdate) -> Entry:
        return store.update_entry(entry_id, data)

    @application.delete("/api/entries/{entry_id}", status_code=204)
    def delete_entry(entry_id: str, revision: int = Query(ge=1)) -> Response:
        store.delete_entry(entry_id, revision)
        return Response(status_code=204)

    @application.post("/api/batches/{request_id}/undo", status_code=204)
    def undo(request_id: UUID) -> Response:
        store.undo(str(request_id))
        return Response(status_code=204)

    @application.get("/api/settings", response_model=Settings)
    def settings() -> Settings:
        return store.settings()

    @application.put("/api/settings", response_model=Settings)
    def update_settings(data: Settings) -> Settings:
        return store.update_settings(data)

    @application.get("/api/export", response_model=ExportData)
    def export() -> ExportData:
        return store.export()

    @application.get("/api/demo")
    def demo_info() -> dict[str, Any]:
        if not demo_config:
            return {"demo": False}
        return {
            "demo": True,
            "reset_minutes": demo_config.reset_minutes,
            "ai_per_hour": demo_config.ai_per_ip_per_hour,
        }

    @application.post("/api/restore/preview")
    def restore_preview(data: RestorePreviewRequest) -> dict[str, int]:
        refuse_in_demo("Restoring a backup")
        return store.validate_restore(data.data)

    @application.post("/api/restore")
    def restore(data: RestoreRequest) -> dict[str, int]:
        refuse_in_demo("Restoring a backup")
        return store.restore(data.data)

    @application.get("/api/starter-drafts")
    def starter_drafts() -> list[dict[str, Any]]:
        if demo_config:
            return []
        path = Path(os.getenv("KCALLM_STARTER_FILE", str(private_dir / "starter-drafts.json")))
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("Expected a list of food drafts.")
        except (OSError, ValueError) as exc:
            raise DomainError(
                "invalid_starter_file", "The private starter file is invalid.", 500
            ) from exc
        return data

    if include_ai:
        from .ai import create_ai_router

        application.include_router(
            create_ai_router(store.catalogue_json, private_dir, demo=demo_config)
        )

    dist = Path(os.getenv("KCALLM_FRONTEND_DIR", str(PROJECT_ROOT / "frontend" / "dist")))
    if dist.is_dir():

        @application.get("/{path:path}", include_in_schema=False, response_model=None)
        def frontend(path: str) -> Response:
            if path == "api" or path.startswith("api/"):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            candidate = (dist / path).resolve()
            if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            if Path(path).suffix:
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            return FileResponse(dist / "index.html")

    return application


app = create_app()
