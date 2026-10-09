"""GitHub App: tokens, GET-only calls, the authenticated clone, and repository discovery."""

import base64

import jwt
import pytest

from app.connectors.github import GitHubError, GitHubWriteRefusedError, check_repo_name
from app.connectors.github_source import GitHubAppSource
from tests.discovery_helpers import INSTALLATION, Recorded, github_app, github_result, settings

pytestmark = pytest.mark.anyio

TOKEN = "ghs_FAKEinstallationTOKEN0123456789abcdefABCD"  # noqa: S105 - from github.json


async def test_installation_token_is_minted_once_and_cached() -> None:
    recorded = Recorded()
    app = github_app(recorded)
    assert await app.installation_token(INSTALLATION) == TOKEN
    assert await app.installation_token(INSTALLATION) == TOKEN
    posts = [p for p in recorded.paths() if p.startswith("POST")]
    assert posts == [f"POST /app/installations/{INSTALLATION}/access_tokens"]
    # The app JWT is RS256, issued by the app id, short-lived.
    claims = jwt.decode(app.app_jwt(), options={"verify_signature": False})
    assert claims["iss"] == "2050123" and claims["exp"] - claims["iat"] <= 600


async def test_only_get_calls_except_minting_tokens() -> None:
    app = github_app()
    for method, path in [
        ("POST", "/repos/o/r/pulls"),
        ("PATCH", "/repos/o/r"),
        ("DELETE", "/repos/o/r/actions/runs/1"),
        ("PUT", "/repos/o/r/contents/x"),
        ("POST", "/repos/o/r/actions/workflows/1/dispatches"),
    ]:
        with pytest.raises(GitHubWriteRefusedError):
            await app._request(method, path, bearer="x")


async def test_installations_and_repos() -> None:
    app = github_app()
    (install,) = await app.installations()
    assert install == {
        "id": INSTALLATION,
        "account": "GroundsCrew-AI",
        "account_type": "Organization",
        "repository_selection": "selected",
    }
    repos = await app.installation_repos(INSTALLATION)
    assert [r["full_name"] for r in repos] == [
        "GroundsCrew-AI/checkout-demo",
        "GroundsCrew-AI/groundcrew",
    ]


async def test_errors_never_include_the_token() -> None:
    app = github_app()
    with pytest.raises(GitHubError) as exc:
        await app.get(INSTALLATION, "/repos/GroundsCrew-AI/missing")
    assert TOKEN not in str(exc.value) and "404" in str(exc.value)


@pytest.mark.parametrize("name", ["../etc", "a/b/c", "-x/y", "a", "a/.."])
def test_repo_names_are_checked(name: str) -> None:
    with pytest.raises(GitHubError):
        check_repo_name(name)


def test_clone_token_goes_through_git_config_env_not_url_or_argv() -> None:
    source = GitHubAppSource(INSTALLATION, "GroundsCrew-AI/checkout-demo", "main", app=github_app())
    clone = source.clone_source(TOKEN)
    assert clone.url == "https://github.com/GroundsCrew-AI/checkout-demo.git"
    assert TOKEN not in clone.url
    env = clone._env()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic}"
    assert TOKEN not in source.describe()
    # The shared settings are untouched: the token lives on a copy.
    assert source.app.settings.git_clone_token is None and settings().git_clone_token is None


def test_materialize_mints_the_installation_token_then_clones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from app.repos.source import GitCloneSource

    recorded = Recorded()
    seen: list[str | None] = []

    def fake_materialize(self: GitCloneSource) -> Path:
        token = self.settings.git_clone_token
        seen.append(token.get_secret_value() if token else None)
        return Path("/tmp/x")  # noqa: S108 - not created

    monkeypatch.setattr(GitCloneSource, "materialize", fake_materialize)
    source = GitHubAppSource(INSTALLATION, "GroundsCrew-AI/checkout-demo", app=github_app(recorded))
    with source as root:
        assert root == Path("/tmp/x")  # noqa: S108
    assert seen == [TOKEN]
    assert recorded.paths() == [f"POST /app/installations/{INSTALLATION}/access_tokens"]


async def test_discovers_repo_and_workflows_with_runs_and_commits() -> None:
    result = await github_result()
    by_type = {r.type: r for r in result.resources}
    repo = by_type["github:repository"]
    assert repo.external_id == "github:repo:groundscrew-ai/checkout-demo"
    assert repo.config["recent_commits"][0]["message"].startswith("Show the Vercel deployment")
    workflow = by_type["github:workflow"]
    assert (
        workflow.name == "deploy (deploy.yml)"
        and workflow.iac_path == ".github/workflows/deploy.yml"
    )
    assert workflow.config["last_run"]["conclusion"] == "success"
    assert [r["path"] for r in workflow.config["recent_runs"]] == [".github/workflows/deploy.yml"]
    (facts,) = result.repos
    assert facts.commit == "cef79e7a1b2c3d4e5f60718293a4b5c6d7e8f901" and len(facts.terraform) > 20


async def test_clone_failure_still_reads_workflows_through_the_api() -> None:
    """Live failure: no git in the image. Deploy edges must survive through the contents API."""
    from app.discovery.github import discover_github
    from app.repos.source import RepoSourceError
    from tests.discovery_helpers import DEMO_REPO

    deploy = (DEMO_REPO / ".github/workflows/deploy.yml").read_text()
    base = "/repos/GroundsCrew-AI/checkout-demo/contents/.github/workflows"
    routes = {
        f"GET {base}": [
            {"path": ".github/workflows/deploy.yml", "type": "file", "size": len(deploy)},
            {"path": ".github/workflows/README.md", "type": "file", "size": 10},
        ],
        f"GET {base}/deploy.yml": deploy,
    }

    async def no_git(_i: int, _n: str, _r: str | None) -> object:
        raise RepoSourceError("git is not installed")

    result = await discover_github(
        github_app(extra_routes=routes),
        INSTALLATION,
        ["GroundsCrew-AI/checkout-demo"],
        open_source=no_git,  # type: ignore[arg-type]
    )
    (facts,) = result.repos
    assert [w.path for w in facts.workflows] == [".github/workflows/deploy.yml"]
    assert facts.terraform == [] and "Clone failed" in facts.notes[0]
    assert any(r.type == "github:workflow" for r in result.resources)


async def test_real_deploy_yml_deploys_api_worker_and_web() -> None:
    from app.graph.edges import infer_edges, key
    from tests.discovery_helpers import demo_inventory

    resources, repos = await demo_inventory()
    names = {key(r): r.name for r in resources}
    deployed = sorted(
        names[e.source]
        for e in infer_edges(resources, repos).edges
        if e.kind == "deployed_by" and names[e.target] == "deploy (deploy.yml)"
    )
    assert deployed == ["checkout-api", "checkout-demo", "checkout-worker"]
