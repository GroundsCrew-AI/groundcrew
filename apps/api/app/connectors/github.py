"""GitHub App access: app JWT, installation tokens, and read-only REST calls.

The app's private key is read from GITHUB_APP_PRIVATE_KEY_PATH. Installation tokens are
minted on demand, cached in memory until five minutes before they expire, and never stored
or logged. Every REST call is a GET, except minting an installation token; anything else is
refused before it is sent (GITHUB_ALLOWED_WRITES).
"""

import asyncio
import contextlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

from app.config import Settings, get_settings
from app.discovery.types import ConnectorError
from app.security.redact import redact

API_VERSION = "2022-11-28"
# The only non-GET call: POST /app/installations/{id}/access_tokens.
GITHUB_ALLOWED_WRITES = (re.compile(r"^/app/installations/\d+/access_tokens$"),)
REFRESH_MARGIN_SECONDS = 300
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*$")


class GitHubError(ConnectorError):
    pass


class GitHubWriteRefusedError(GitHubError):
    """Code tried a non-GET call other than minting an installation token."""


def check_repo_name(full_name: str) -> str:
    if not _REPO.match(full_name) or full_name.split("/", 1)[1] in (".", ".."):
        raise GitHubError(f"Not a repository name (owner/name): {full_name!r}")
    return full_name


@dataclass
class _Token:
    value: str
    expires_at: float


class GitHubApp:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        private_key: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport
        self._private_key = private_key
        self._tokens: dict[int, _Token] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth

    def configured(self) -> bool:
        key = self._private_key or self.settings.github_app_private_key_path
        return bool(self.settings.github_app_id and key)

    def _key(self) -> str:
        if self._private_key is not None:
            return self._private_key
        path = self.settings.github_app_private_key_path
        if not self.settings.github_app_id or path is None:
            raise GitHubError("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be set.")
        if not path.is_absolute():
            # Relative to the working directory (/app in the containers, where ./secrets is
            # mounted), else to the repo root, where .env lives.
            here = Path(__file__).resolve()
            roots = [Path.cwd(), *([here.parents[4]] if len(here.parents) > 4 else [])]
            path = next((r / path for r in roots if (r / path).is_file()), roots[-1] / path)
        try:
            self._private_key = path.read_text()
        except OSError:
            raise GitHubError(f"Cannot read the GitHub App private key at {path}.") from None
        return self._private_key

    def app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.settings.github_app_id)}
        return jwt.encode(payload, self._key(), algorithm="RS256")

    def installation_token_sync(self, installation_id: int) -> str:
        """Blocking: for code already in a worker thread (GitHubAppSource.materialize)."""
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - REFRESH_MARGIN_SECONDS > time.time():
                return cached.value
            data = self._request_sync(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                bearer=self.app_jwt(),
            )
            token = str(data["token"])
            expires = data.get("expires_at")
            expires_at = (
                datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
                if expires
                else time.time() + 3600
            )
            self._tokens[installation_id] = _Token(token, expires_at)
            return token

    async def installation_token(self, installation_id: int) -> str:
        return await asyncio.to_thread(self.installation_token_sync, installation_id)

    # ---------------------------------------------------------------- transport

    def _headers(self, method: str, path: str, bearer: str, accept: str) -> dict[str, str]:
        if method != "GET" and not any(p.match(path) for p in GITHUB_ALLOWED_WRITES):
            raise GitHubWriteRefusedError(f"Groundcrew does not call {method} {path} on GitHub.")
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "groundcrew",
        }

    def _parse(self, method: str, path: str, response: httpx.Response, accept: str) -> Any:
        if response.status_code >= 400:
            message = ""
            with contextlib.suppress(ValueError):
                message = str(response.json().get("message", ""))
            raise GitHubError(
                redact(f"GitHub {method} {path} returned {response.status_code}: {message}")
            )
        if accept.endswith("json"):
            return response.json()
        return response.text

    async def _request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        headers = self._headers(method, path, bearer, accept)
        async with httpx.AsyncClient(
            base_url=self.settings.github_api_url, transport=self._transport, timeout=20
        ) as client:
            try:
                response = await client.request(method, path, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise GitHubError(f"GitHub {path}: {type(exc).__name__}") from None
        return self._parse(method, path, response, accept)

    def _request_sync(self, method: str, path: str, *, bearer: str) -> Any:
        accept = "application/vnd.github+json"
        headers = self._headers(method, path, bearer, accept)
        sync = self._transport if isinstance(self._transport, httpx.BaseTransport) else None
        with httpx.Client(
            base_url=self.settings.github_api_url, transport=sync, timeout=20
        ) as client:
            try:
                response = client.request(method, path, headers=headers)
            except httpx.HTTPError as exc:
                raise GitHubError(f"GitHub {path}: {type(exc).__name__}") from None
        return self._parse(method, path, response, accept)

    async def app_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, bearer=self.app_jwt(), params=params)

    async def get(
        self, installation_id: int, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        token = await self.installation_token(installation_id)
        return await self._request("GET", path, bearer=token, params=params)

    # ---------------------------------------------------------------- reads

    async def installations(self) -> list[dict[str, Any]]:
        data = await self.app_get("/app/installations", {"per_page": 100})
        return [
            {
                "id": int(i["id"]),
                "account": i["account"]["login"],
                "account_type": i["account"].get("type"),
                "repository_selection": i.get("repository_selection"),
            }
            for i in data
        ]

    async def installation_repos(self, installation_id: int) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        while page <= 10:
            data = await self.get(
                installation_id, "/installation/repositories", {"per_page": 100, "page": page}
            )
            batch = data.get("repositories", [])
            repos += [
                {
                    "full_name": r["full_name"],
                    "private": bool(r.get("private")),
                    "default_branch": r.get("default_branch"),
                    "html_url": r.get("html_url"),
                    "description": r.get("description"),
                    "pushed_at": r.get("pushed_at"),
                }
                for r in batch
            ]
            if len(batch) < 100:
                break
            page += 1
        return repos

    async def workflow_files(
        self, installation_id: int, full_name: str, ref: str | None = None
    ) -> list[tuple[str, str]]:
        """(path, text) of .github/workflows/*.yml through the contents API (GET only), for
        when a clone is not possible. Files over 512 KB are skipped."""
        name = check_repo_name(full_name)
        params = {"ref": ref} if ref else None
        try:
            listing = await self.get(
                installation_id, f"/repos/{name}/contents/.github/workflows", params
            )
        except GitHubError as exc:
            if "404" in str(exc):
                return []
            raise
        out: list[tuple[str, str]] = []
        for item in listing if isinstance(listing, list) else []:
            path = str(item.get("path") or "")
            if item.get("type") != "file" or not path.endswith((".yml", ".yaml")):
                continue
            if int(item.get("size") or 0) > 512_000:
                continue
            token = await self.installation_token(installation_id)
            text = await self._request(
                "GET",
                f"/repos/{name}/contents/{path}",
                bearer=token,
                params=params,
                accept="application/vnd.github.raw",
            )
            out.append((path, str(text)))
        return out

    async def repo(self, installation_id: int, full_name: str) -> dict[str, Any]:
        data: dict[str, Any] = await self.get(
            installation_id, f"/repos/{check_repo_name(full_name)}"
        )
        return data

    async def recent_commits(
        self, installation_id: int, full_name: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        data = await self.get(
            installation_id, f"/repos/{check_repo_name(full_name)}/commits", {"per_page": limit}
        )
        return [
            {
                "sha": c["sha"],
                "message": str((c.get("commit") or {}).get("message", "")).split("\n", 1)[0][:200],
                "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
                "at": ((c.get("commit") or {}).get("committer") or {}).get("date"),
                "html_url": c.get("html_url"),
            }
            for c in data
        ]

    async def recent_runs(
        self, installation_id: int, full_name: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        data = await self.get(
            installation_id,
            f"/repos/{check_repo_name(full_name)}/actions/runs",
            {"per_page": limit},
        )
        return [
            {
                "id": r["id"],
                "name": r.get("name"),
                "path": r.get("path"),
                "event": r.get("event"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "head_sha": r.get("head_sha"),
                "head_branch": r.get("head_branch"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "html_url": r.get("html_url"),
            }
            for r in data.get("workflow_runs", [])
        ]


_app: GitHubApp | None = None


def get_github_app() -> GitHubApp:
    global _app
    if _app is None:
        _app = GitHubApp()
    return _app
