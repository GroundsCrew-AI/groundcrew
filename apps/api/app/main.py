import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

from app.config import get_settings
from app.db.session import get_engine
from app.log import request_id_var, setup_logging
from app.routes import (
    approvals,
    connections,
    deploys,
    evidence,
    graph,
    incidents,
    runs,
    workspaces,
)
from app.routes.schemas import ApprovalStepEvent, RunStatusEvent, StepOut

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging(get_settings().log_level)
    yield
    if get_settings().database_url is not None:
        await get_engine().dispose()


app = FastAPI(title="Groundcrew API", version=VERSION, lifespan=lifespan)

# REST calls come through the web app's server-side proxy, so they need no CORS. Only the
# browser's direct SSE connection (GET /runs/{id}/events) does, and only from WEB_ORIGIN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().web_origin],
    allow_methods=["GET"],
    allow_headers=["Last-Event-ID"],
)
app.add_middleware(RequestIdMiddleware)

# Request fields whose raw input must never come back in a 422 (POST /deploys env values).
_SECRET_INPUT_FIELDS = frozenset({"env"})


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


# Every route except /health and the SSE stream requires the signed-in user (CurrentUser).
app.include_router(workspaces.router)
app.include_router(runs.router)
app.include_router(approvals.router)
app.include_router(connections.router)
app.include_router(graph.router)
app.include_router(deploys.router)
app.include_router(incidents.router)
app.include_router(evidence.router)


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
    return Health(status="ok", version=VERSION)
