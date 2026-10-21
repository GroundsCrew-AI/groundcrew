"""API responses carry security headers; CORS allows only the web app's origin, for reading
live streams; a hosted instance does not serve the interactive docs or the schema."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.main import app

pytestmark = pytest.mark.anyio


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_every_response_has_the_headers() -> None:
    async with await _client() as c:
        for r in (await c.get("/health"), await c.get("/runs/not-a-uuid")):
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert (
                r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
            )
            assert r.headers["cache-control"] == "no-store"


async def test_cors_allows_only_the_web_app_and_only_reads() -> None:
    web = get_settings().web_origin
    async with await _client() as c:
        ok = await c.options(
            "/runs/x/events",
            headers={"Origin": web, "Access-Control-Request-Method": "GET"},
        )
        assert ok.headers.get("access-control-allow-origin") == web
        other = await c.options(
            "/runs/x/events",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in other.headers
        post = await c.options(
            "/runs",
            headers={"Origin": web, "Access-Control-Request-Method": "POST"},
        )
        assert post.status_code == 400
        plain = await c.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in plain.headers


def test_a_hosted_instance_has_no_docs() -> None:
    code = "from app.main import app; print(app.docs_url, app.redoc_url, app.openapi_url)"
    env = {**os.environ, "APP_ENV": "production"}
    out = subprocess.run(  # noqa: S603 - a fixed command
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split() == ["None", "None", "None"]
    assert app.docs_url == "/docs"  # tests run as APP_ENV=test
