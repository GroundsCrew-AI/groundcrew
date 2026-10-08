"""A repository read through our GitHub App (docs/phase3-interfaces.md, `GitHubAppSource`).

Reuses GitCloneSource unchanged: the installation token goes into a copy of the settings as
`git_clone_token`, so every GitCloneSource rule still holds (https only, shallow, working tree
only, REPO_MAX_BYTES, always cleaned up). The token reaches git as an `http.extraHeader`
through git's environment (GIT_CONFIG_KEY_0 / GIT_CONFIG_VALUE_0), the same setting as
`git -c http.extraHeader=...` but without putting it in argv, where any local user could read
it from the process list. It is never in the URL, an error, or a log line.
"""

from pathlib import Path

from pydantic import SecretStr

from app.config import Settings, get_settings
from app.connectors.github import GitHubApp, check_repo_name
from app.repos.source import GitCloneSource


class GitHubAppSource:
    def __init__(
        self,
        full_name: str,
        token: str,
        *,
        ref: str | None = None,
        subpath: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        base = settings or get_settings()
        self.full_name = check_repo_name(full_name)
        scoped = base.model_copy(update={"git_clone_token": SecretStr(token)})
        # api.github.com -> github.com; GitHub Enterprise https://host/api/v3 -> https://host
        api = base.github_api_url.rstrip("/")
        host = (
            "https://github.com" if api == "https://api.github.com" else api.removesuffix("/api/v3")
        )
        self._clone = GitCloneSource(
            f"{host}/{self.full_name}.git", ref=ref, subpath=subpath, settings=scoped
        )

    @classmethod
    async def create(
        cls,
        app: GitHubApp,
        installation_id: int,
        full_name: str,
        *,
        ref: str | None = None,
        subpath: str | None = None,
    ) -> "GitHubAppSource":
        token = await app.installation_token(installation_id)
        return cls(full_name, token, ref=ref, subpath=subpath, settings=app.settings)

    @property
    def commit(self) -> str | None:
        return self._clone.commit

    def materialize(self) -> Path:
        return self._clone.materialize()

    def describe(self) -> str:
        return f"GitHub App {self._clone.describe()}"

    def cleanup(self) -> None:
        self._clone.cleanup()

    def __enter__(self) -> Path:
        return self.materialize()

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()
