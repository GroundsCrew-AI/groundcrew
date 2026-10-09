"""A repository read through our GitHub App: the RepoSource contract in
docs/phase3-interfaces.md, `GitHubAppSource(installation_id, repo, ref)`.

Reuses GitCloneSource unchanged, so every one of its rules still holds: https only, shallow,
working tree only, REPO_MAX_BYTES, the clone timeout, and the temp dir is always deleted.
`materialize()` mints an installation token (cached in memory by GitHubApp) and puts it into
a copy of the settings as `git_clone_token`. The token reaches git as an `http.extraHeader`
through git's environment (GIT_CONFIG_KEY_0 / GIT_CONFIG_VALUE_0), the same setting as
`git -c http.extraHeader=...` but without putting it in argv, where any local user could read
it from the process list. It is never in the URL, an error, or a log line.

`materialize()` blocks (token request and clone): call it with `asyncio.to_thread`.
"""

from pathlib import Path

from pydantic import SecretStr

from app.connectors.github import GitHubApp, check_repo_name, get_github_app
from app.repos.source import GitCloneSource


class GitHubAppSource:
    def __init__(
        self,
        installation_id: int,
        repo: str,
        ref: str | None = None,
        *,
        subpath: str | None = None,
        app: GitHubApp | None = None,
    ) -> None:
        self.installation_id = installation_id
        self.repo = check_repo_name(repo)
        self.ref = ref
        self.subpath = subpath
        self.app = app or get_github_app()
        self._clone: GitCloneSource | None = None

    @property
    def url(self) -> str:
        # api.github.com -> github.com; GitHub Enterprise https://host/api/v3 -> https://host
        api = self.app.settings.github_api_url.rstrip("/")
        host = (
            "https://github.com" if api == "https://api.github.com" else api.removesuffix("/api/v3")
        )
        return f"{host}/{self.repo}.git"

    def clone_source(self, token: str) -> GitCloneSource:
        """The GitCloneSource that does the work, with the token on a settings copy."""
        scoped = self.app.settings.model_copy(update={"git_clone_token": SecretStr(token)})
        return GitCloneSource(self.url, ref=self.ref, subpath=self.subpath, settings=scoped)

    @property
    def commit(self) -> str | None:
        return self._clone.commit if self._clone is not None else None

    def materialize(self) -> Path:
        self.cleanup()
        self._clone = self.clone_source(self.app.installation_token_sync(self.installation_id))
        return self._clone.materialize()

    def describe(self) -> str:
        ref = f" @ {self.ref}" if self.ref else ""
        return f"GitHub App installation {self.installation_id}: {self.repo}{ref}"

    def cleanup(self) -> None:
        if self._clone is not None:
            self._clone.cleanup()

    def __enter__(self) -> Path:
        return self.materialize()

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()
