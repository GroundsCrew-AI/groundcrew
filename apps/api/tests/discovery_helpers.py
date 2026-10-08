"""Fixtures for discovery tests: recorded HTTP responses (GitHub, Vercel) served by
httpx.MockTransport, a local checkout of the demo repository, and the whole demo inventory
(AWS + GitHub + Vercel) built by the real connectors."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.connectors.aws import AwsReader
from app.connectors.github import GitHubApp
from app.connectors.vercel import VercelClient
from app.discovery.aws import AwsDiscovery
from app.discovery.github import discover_github
from app.discovery.types import ConnectorResult, DiscoveredResource, RepoFacts
from app.discovery.vercel import discover_vercel
from tests.aws_fixture import ACCOUNT, fixture_session

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
DEMO_REPO = FIXTURES / "repos" / "checkout-demo"
INSTALLATION = 168436777
TEAM = "team_groundcrew"
FAKE_VERCEL_TOKEN = "vercel_FAKE_token_0123456789"  # noqa: S105


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@dataclass
class Recorded:
    """Requests a MockTransport saw."""

    requests: list[httpx.Request] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def mock_transport(routes: dict[str, Any], recorded: Recorded | None = None) -> httpx.MockTransport:
    seen = recorded if recorded is not None else Recorded()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.requests.append(request)
        body = routes.get(f"{request.method} {request.url.path}")
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(body, str):  # a raw file
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "github_app_id": "2050123",
        "discovery_regions": ["us-east-2"],
        "model_nano": "test/nano",
        **overrides,
    }
    return Settings(_env_file=None, **values)


class LocalCheckout:
    """Stands in for GitHubAppSource: the fixture repository, never deleted."""

    commit = "cef79e7a1b2c3d4e5f60718293a4b5c6d7e8f901"

    def __init__(self, root: Path = DEMO_REPO) -> None:
        self.root = root

    def __enter__(self) -> Path:
        return self.root

    def __exit__(self, *_exc: object) -> None:
        return None


async def open_fixture_repo(_installation: int, _name: str, _ref: str | None) -> LocalCheckout:
    return LocalCheckout()


def github_app(
    recorded: Recorded | None = None, extra_routes: dict[str, Any] | None = None
) -> GitHubApp:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return GitHubApp(
        settings(),
        transport=mock_transport({**load("github.json"), **(extra_routes or {})}, recorded),
        private_key=pem,
    )


def vercel_client(
    routes: dict[str, Any] | None = None, recorded: Recorded | None = None
) -> VercelClient:
    return VercelClient(
        FAKE_VERCEL_TOKEN,
        TEAM,
        settings(),
        transport=mock_transport(load("vercel.json") if routes is None else routes, recorded),
    )


async def aws_result(responses: dict[str, Any] | None = None) -> ConnectorResult:
    reader = AwsReader(fixture_session(responses), settings())
    return await AwsDiscovery(reader, ACCOUNT, max_resources=2000).run()


async def github_result() -> ConnectorResult:
    return await discover_github(
        github_app(), INSTALLATION, ["GroundsCrew-AI/checkout-demo"], open_source=open_fixture_repo
    )


async def vercel_result(routes: dict[str, Any] | None = None) -> ConnectorResult:
    return await discover_vercel(vercel_client(routes))


async def demo_inventory(
    vercel_routes: dict[str, Any] | None = None,
) -> tuple[list[DiscoveredResource], list[RepoFacts]]:
    aws, gh, vc = await aws_result(), await github_result(), await vercel_result(vercel_routes)
    return [*aws.resources, *gh.resources, *vc.resources], gh.repos


def plain_api_url_routes(value: str) -> dict[str, Any]:
    """vercel.json with API_URL stored as a plain variable holding `value`."""
    routes = load("vercel.json")
    for env in routes["GET /v10/projects/prj_checkout/env"]["envs"]:
        if env["key"] == "API_URL":
            env["type"] = "plain"
            env["value"] = value
    return routes
