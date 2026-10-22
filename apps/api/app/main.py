import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.accounts import ForbiddenError
from app.config import get_settings
from app.db.session import get_engine
from app.log import request_id_var, setup_logging
from app.quotas import QuotaExceededError
from app.remote_agents import routes as remote_agents
from app.routes import (
    admin,
    approvals,
    auth,
    connections,
    deploys,
    evidence,
    github_webhook,
    graph,
    incidents,
    internal,
    runs,
    workspace_admin,
    workspaces,
)
from app.routes.schemas import ApprovalStepEvent, RunStatusEvent, StepOut
from app.writable import check_writable

VERSION = "0.1.0"

logger = logging.getLogger("groundcrew.api")

# Accept a caller's X-Request-ID only if it is short and plain.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RequestIdMiddleware:
    """Give every request an id (or keep a valid incoming X-Request-ID), put it on every log
    line and on the response, and log one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000),
                },
            )
            request_id_var.reset(token)


# On every API response. The API serves JSON and event streams only, so nothing may run, load,
# or frame it. HSTS comes from nginx.
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-resource-policy", b"same-site"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in SECURITY_HEADERS if n not in present]
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, with_headers)


class BodySizeLimitMiddleware:
    """Refuse request bodies over MAX_REQUEST_BYTES with 413: by Content-Length up front, and
    while reading for bodies sent without one. nginx has the same limit in front of this."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_request_bytes
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            await _too_large(send, limit)
            return
        seen = 0
        refused = False

        async def counted() -> Message:
            # Past the limit: answer 413 now and tell the app the client went away.
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit and not refused:
                    refused = True
                    await _too_large(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def guarded(message: Message) -> None:
            if not refused:
                await send(message)

        await self.app(scope, counted, guarded)


async def _too_large(send: Send, limit: int) -> None:
    body = json.dumps({"detail": f"The request is larger than {limit // 1024} KB."}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    settings.require_production_ready()
    check_writable(settings)
    yield
    if get_settings().database_url is not None:
        await get_engine().dispose()


# The interactive docs and the schema are for development; a hosted instance does not serve them
# (the web app's types come from scripts/export_openapi.py, not from a running server).
_LOCAL = get_settings().app_env in ("dev", "test")
app = FastAPI(
    title="Groundcrew API",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _LOCAL else None,
    redoc_url="/redoc" if _LOCAL else None,
    openapi_url="/openapi.json" if _LOCAL else None,
)

# REST calls come through the web app's server-side proxy, so they need no CORS. Only the
# browser's direct SSE connection (GET /runs/{id}/events) does, and only from WEB_ORIGIN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().web_origin],
    allow_methods=["GET"],
    allow_headers=["Last-Event-ID"],
)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)

# Request fields whose raw input must never come back in a 422 (POST /deploys env values,
# passwords, email link tokens).
_SECRET_INPUT_FIELDS = frozenset({"env", "password", "current_password", "new_password", "token"})


@app.exception_handler(ForbiddenError)
async def forbidden(_request: Request, exc: ForbiddenError) -> JSONResponse:
    """Owner-only actions by a member: 403 with the reason (they can see the workspace)."""
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(QuotaExceededError)
async def quota_exceeded(_request: Request, exc: QuotaExceededError) -> JSONResponse:
    """A workspace quota: 429 with which one, its limit, and when it resets (if daily)."""
    headers = {}
    if exc.resets_at is not None:
        wait = max(1, int((exc.resets_at - datetime.now(UTC)).total_seconds()))
        headers["Retry-After"] = str(wait)
    return JSONResponse(
        status_code=429,
        headers=headers,
        content={
            "detail": str(exc),
            "code": "quota_exceeded",
            "quota": exc.quota,
            "limit": exc.limit,
            "resets_at": exc.resets_at.isoformat() if exc.resets_at else None,
        },
    )


@app.exception_handler(auth.AuthFlowError)
async def auth_failure(_request: Request, exc: auth.AuthFlowError) -> JSONResponse:
    """Sign-in failures carry a code the web app maps to the next step (confirm your email,
    reset your password, wait)."""
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code}
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, minus the echoed input for secret fields: a too-long or
    duplicated env var would otherwise send its value back through the web proxy."""
    errors = []
    for error in exc.errors():
        loc = error.get("loc", ())
        if len(loc) > 1 and loc[0] == "body" and loc[1] in _SECRET_INPUT_FIELDS:
            error = {k: v for k, v in error.items() if k not in ("input", "ctx")}
        errors.append(error)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


# Every route except /health, /ready, and the SSE stream requires the signed-in user
# (CurrentUser); /auth requires the web server's service token instead, and /agent/v1 a
# request signed by an enrolled agent (app/remote_agents).
app.include_router(auth.router)
app.include_router(workspaces.router)
app.include_router(workspace_admin.router)
app.include_router(runs.router)
app.include_router(approvals.router)
app.include_router(connections.router)
app.include_router(graph.router)
app.include_router(deploys.router)
app.include_router(incidents.router)
app.include_router(evidence.router)
app.include_router(admin.router)
app.include_router(github_webhook.router)
app.include_router(remote_agents.router)
app.include_router(internal.router)


def _openapi() -> dict[str, Any]:
    """The OpenAPI schema plus the SSE payload models, which no route returns as JSON, so the
    web app's generated client has types for the live stream too."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    _, defs = models_json_schema(
        [(m, "serialization") for m in (StepOut, RunStatusEvent, ApprovalStepEvent)],
        ref_template="#/components/schemas/{model}",
    )
    schema.setdefault("components", {}).setdefault("schemas", {}).update(defs.get("$defs", {}))
    app.openapi_schema = schema
    return schema


app.openapi = _openapi  # type: ignore[method-assign]


class Health(BaseModel):
    status: str
    version: str


@app.get("/health")
def health() -> Health:
    """Liveness: the process answers. Use /ready for traffic and deploy checks."""
    return Health(status="ok", version=VERSION)


class Ready(BaseModel):
    status: str
    version: str
    database: str
    migrations: str


@app.get("/ready", responses={503: {"model": Ready}})
async def ready() -> JSONResponse:
    """Readiness: the database answers and its schema is at this build's migration head."""
    from app.db.readiness import check

    database, migrations = await check()
    ok = database == "ok" and migrations == "ok"
    body = Ready(
        status="ok" if ok else "unavailable",
        version=VERSION,
        database=database,
        migrations=migrations,
    )
    return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())
