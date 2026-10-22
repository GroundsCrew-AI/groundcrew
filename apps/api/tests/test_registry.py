"""The Groundcrew registry (app/registry.py): pull tokens scoped to one workspace, nginx's
access check, the internal auth route, and (opt in, GC_DOCKER_TEST=1) an upload of a real
`docker save` to a real registry container."""

import base64
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app import registry
from app.config import get_settings
from app.main import app
from tests.test_api import _settings

pytestmark = pytest.mark.anyio

WS = uuid.UUID("12345678-1234-5678-1234-567812345678")
OTHER = uuid.UUID("87654321-4321-8765-4321-876543218765")


def basic(password: str, user: str = registry.USERNAME) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_a_pull_token_reads_only_its_workspace_and_only_pulls() -> None:
    s = _settings()
    token = registry.pull_token(s, WS)
    ns = registry.namespace(WS)
    assert registry.token_namespace(s, token) == ns
    ok = basic(token)
    assert registry.check_access(s, ok, "/v2/", "GET")
    assert registry.check_access(s, ok, f"/v2/{ns}/web/manifests/abc123", "HEAD")
    assert registry.check_access(s, ok, f"/v2/{ns}/web/blobs/sha256:{'a' * 64}", "GET")
    other = registry.namespace(OTHER)
    assert not registry.check_access(s, ok, f"/v2/{other}/web/manifests/abc123", "GET")
    assert not registry.check_access(s, ok, f"/v2/{ns}/web/blobs/uploads/", "POST")
    assert not registry.check_access(s, ok, "/v2/_catalog", "GET")
    assert not registry.check_access(s, basic(token, "admin"), "/v2/", "GET")
    assert not registry.check_access(s, None, "/v2/", "GET")


def test_expired_or_forged_tokens_are_refused() -> None:
    s = _settings()
    old = registry.pull_token(s, WS, now=time.time() - 3600)
    assert registry.token_namespace(s, old) is None
    token = registry.pull_token(s, WS)
    _, _, mac = token.removeprefix(registry.TOKEN_PREFIX).partition(".")
    forged_body = (
        base64.urlsafe_b64encode(
            f'{{"ns":"{registry.namespace(OTHER)}","exp":{int(time.time()) + 600}}}'.encode()
        )
        .decode()
        .rstrip("=")
    )
    assert registry.token_namespace(s, f"{registry.TOKEN_PREFIX}{forged_body}.{mac}") is None
    assert registry.token_namespace(s, "gcr_nonsense") is None
    other_key = _settings(approval_token_key="another-approval-key-0123456789abcdef")  # noqa: S106
    assert registry.token_namespace(other_key, token) is None


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_settings] = lambda: _settings()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def test_the_internal_auth_route_answers_nginx(client: httpx.AsyncClient) -> None:
    token = registry.pull_token(_settings(), WS)
    ns = registry.namespace(WS)
    ok = await client.get(
        "/internal/registry/auth",
        headers={
            "Authorization": basic(token),
            "X-Original-URI": f"/v2/{ns}/web/manifests/t1",
            "X-Original-Method": "GET",
        },
    )
    assert ok.status_code == 200
    refused = await client.get(
        "/internal/registry/auth",
        headers={"X-Original-URI": f"/v2/{ns}/web/manifests/t1", "X-Original-Method": "GET"},
    )
    assert refused.status_code == 401


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)  # noqa: S603, S607


def test_a_saved_image_uploads_and_pulls_back() -> None:
    if os.environ.get("GC_DOCKER_TEST") != "1":
        pytest.skip("set GC_DOCKER_TEST=1 to use the local Docker engine")
    name = f"gc-registry-test-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--name", name, "-p", "127.0.0.1::5000", "registry:3.0.0")
    try:
        port = _docker("port", name, "5000").stdout.strip().rsplit(":", 1)[-1]
        time.sleep(1.5)
        _docker("pull", "-q", "nginx:1.27-alpine")
        repo = registry.repository(WS, "web")
        url = f"http://127.0.0.1:{port}"
        save = ["docker", "save", "nginx:1.27-alpine"]
        digest = registry.push_saved_image(save, dict(os.environ), url, repo, "t1")
        assert digest.startswith("sha256:")
        pulled = _docker("pull", "-q", f"127.0.0.1:{port}/{repo}:t1", check=False)
        assert pulled.returncode == 0, pulled.stderr
    finally:
        _docker("rm", "-f", name, check=False)
