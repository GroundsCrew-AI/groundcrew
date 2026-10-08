"""Vercel: names and types only, values only for plain *_URL / *_HOST, GET-only, no decrypt."""

import json

import pytest

from app.connectors.vercel import VercelError, VercelReadRefusedError, may_read_value, parse_env
from tests.discovery_helpers import (
    FAKE_VERCEL_TOKEN,
    Recorded,
    load,
    plain_api_url_routes,
    vercel_client,
    vercel_result,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("name", "env_type", "allowed"),
    [
        ("API_URL", "plain", True),
        ("REDIS_HOST", "plain", True),
        ("API_URL", "encrypted", False),
        ("API_URL", "sensitive", False),
        ("API_URL", "secret", False),
        ("STRIPE_KEY", "plain", False),
        ("URL_PREFIX", "plain", False),
    ],
)
def test_may_read_value(name: str, env_type: str, allowed: bool) -> None:
    assert may_read_value(name, env_type) is allowed


def test_parse_env_drops_every_other_value() -> None:
    envs = load("vercel.json")["GET /v10/projects/prj_checkout/env"]["envs"]
    parsed = {e["key"]: e for e in parse_env(envs)}
    assert parsed["SITE_URL"]["value"] == "https://checkout-demo-nu.vercel.app"
    for key in ("API_URL", "STRIPE_KEY", "DB_HOST", "WEBHOOK_URL"):
        assert "value" not in parsed[key], key
    dumped = json.dumps(parse_env(envs))
    assert "sk_live" not in dumped and "ZW5jcnlwdGVk" not in dumped and "c2VjcmV0" not in dumped


async def test_discovery_keeps_names_types_and_allowed_values_only() -> None:
    recorded = Recorded()
    from app.discovery.vercel import discover_vercel

    result = await discover_vercel(vercel_client(recorded=recorded))
    project = next(r for r in result.resources if r.type == "vercel:project")
    assert project.name == "checkout-demo" and project.account == "ground-crew1"
    assert project.config["env_values"] == {"SITE_URL": "https://checkout-demo-nu.vercel.app"}
    assert {e["key"]: e["type"] for e in project.config["env"]}["API_URL"] == "encrypted"
    assert "sk_live" not in json.dumps([r.config for r in result.resources])
    # Never the decrypting endpoint, never decrypt=, always the team, always GET.
    for request in recorded.requests:
        assert request.method == "GET"
        assert "/env/" not in request.url.path
        assert "decrypt" not in request.url.params
        assert request.url.params.get("teamId") == "team_groundcrew"


async def test_latest_production_deployment_only() -> None:
    result = await vercel_result()
    (deployment,) = [r for r in result.resources if r.type == "vercel:deployment"]
    assert deployment.external_id == "vercel:deployment:prj_checkout:production"
    assert deployment.config["state"] == "READY" and deployment.config["uid"] == "dpl_123"


async def test_plain_api_url_value_is_kept() -> None:
    result = await vercel_result(plain_api_url_routes("http://3.15.10.20:8000"))
    project = next(r for r in result.resources if r.type == "vercel:project")
    assert project.config["env_values"]["API_URL"] == "http://3.15.10.20:8000"


async def test_refuses_paths_outside_the_allowlist() -> None:
    client = vercel_client()
    for path in ("/v1/projects/prj_checkout/env/env_api", "/v13/deployments", "/v9/projects/p/env"):
        with pytest.raises(VercelReadRefusedError):
            await client._get(path)
    with pytest.raises(VercelReadRefusedError):
        await client._get("/v10/projects/prj_checkout/env", {"decrypt": "true"})


async def test_token_never_in_errors() -> None:
    client = vercel_client(routes={})
    with pytest.raises(VercelError) as exc:
        await client.whoami()
    assert FAKE_VERCEL_TOKEN not in str(exc.value)
