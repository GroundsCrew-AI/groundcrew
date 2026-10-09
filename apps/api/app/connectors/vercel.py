"""Vercel access with a team-scoped token the user pastes (stored encrypted).

Read-only: projects, their linked Git repository, production domains, recent production
deployments, and environment variable NAMES and TYPES.

Values: the only value ever kept is a variable whose type is "plain" AND whose name ends in
_URL or _HOST (an endpoint, which edge inference compares with real IPs and hostnames). Every
other value is dropped the moment the list response is parsed, before it is stored, logged,
or returned. The decrypting endpoint (/projects/{id}/env/{envId}) and the `decrypt` parameter
are never used; `_get` refuses both, and refuses any method but GET.
"""

import contextlib
import re
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.discovery.types import ConnectorError
from app.security.redact import redact

# Paths we may GET. Anything else is refused before the request is sent.
VERCEL_READ_PATHS = (
    re.compile(r"^/v2/teams/[\w-]+$"),
    re.compile(r"^/v2/user$"),
    re.compile(r"^/v9/projects$"),
    re.compile(r"^/v9/projects/[\w.-]+$"),
    re.compile(r"^/v9/projects/[\w.-]+/domains$"),
    re.compile(r"^/v10/projects/[\w.-]+/env$"),
    re.compile(r"^/v6/deployments$"),
)
VALUE_SUFFIXES = ("_URL", "_HOST")


class VercelError(ConnectorError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status from Vercel, when there was a response


class VercelReadRefusedError(VercelError):
    """Code tried a call outside the read-only allowlist."""


def may_read_value(name: str, env_type: str | None) -> bool:
    return env_type == "plain" and name.upper().endswith(VALUE_SUFFIXES)


def parse_env(envs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Names, types, and targets. A value survives only when may_read_value() says so."""
    out: list[dict[str, Any]] = []
    for env in envs:
        name = str(env.get("key", ""))
        env_type = env.get("type")
        item: dict[str, Any] = {
            "key": name,
            "type": env_type,
            "target": env.get("target") or [],
            "updated_at": env.get("updatedAt"),
        }
        if may_read_value(name, env_type) and isinstance(env.get("value"), str):
            item["value"] = env["value"]
        out.append(item)
    return out


class VercelClient:
    def __init__(
        self,
        token: str,
        team_id: str | None,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise VercelError("A Vercel token is required.")
        self._token = token
        self.team_id = team_id or None
        self.settings = settings or get_settings()
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not any(p.match(path) for p in VERCEL_READ_PATHS):
            raise VercelReadRefusedError(f"Groundcrew does not call GET {path} on Vercel.")
        query = dict(params or {})
        if "decrypt" in query:
            raise VercelReadRefusedError("Groundcrew never asks Vercel to decrypt values.")
        if self.team_id:
            query["teamId"] = self.team_id
        async with httpx.AsyncClient(
            base_url=self.settings.vercel_api_url, transport=self._transport, timeout=20
        ) as client:
            try:
                response = await client.get(
                    path,
                    params=query,
                    headers={"Authorization": f"Bearer {self._token}", "User-Agent": "groundcrew"},
                )
            except httpx.HTTPError as exc:
                raise VercelError(f"Vercel {path}: {type(exc).__name__}") from None
        if response.status_code >= 400:
            message = ""
            with contextlib.suppress(ValueError, AttributeError):
                message = str((response.json().get("error") or {}).get("message", ""))
            text = f"Vercel {path} returned {response.status_code}: {message}"
            raise VercelError(
                redact(text.replace(self._token, "[REDACTED]")), status=response.status_code
            )
        return response.json()

    async def whoami(self) -> dict[str, Any]:
        """The team (when team-scoped) or the user. Proves the token works."""
        if self.team_id:
            team = await self._get(f"/v2/teams/{self.team_id}")
            return {
                "kind": "team",
                "id": team.get("id"),
                "slug": team.get("slug"),
                "name": team.get("name"),
            }
        user = (await self._get("/v2/user")).get("user", {})
        return {
            "kind": "user",
            "id": user.get("id"),
            "slug": user.get("username"),
            "name": user.get("name"),
        }

    async def projects(self, limit: int = 200) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": 100}
        while len(projects) < limit:
            data = await self._get("/v9/projects", params)
            projects += data.get("projects", [])
            nxt = (data.get("pagination") or {}).get("next")
            if not nxt:
                break
            params = {"limit": 100, "until": nxt}
        return projects[:limit]

    async def domains(self, project_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/v9/projects/{project_id}/domains", {"production": "true"})
        return [
            {"name": d.get("name"), "verified": d.get("verified"), "redirect": d.get("redirect")}
            for d in data.get("domains", [])
        ]

    async def env(self, project_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/v10/projects/{project_id}/env")
        return parse_env(data.get("envs", []))

    async def production_deployments(self, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get(
            "/v6/deployments", {"projectId": project_id, "target": "production", "limit": limit}
        )
        out = []
        for d in data.get("deployments", []):
            meta = d.get("meta") or {}
            out.append(
                {
                    "uid": d.get("uid"),
                    "url": d.get("url"),
                    "state": d.get("state") or d.get("readyState"),
                    "created_at": d.get("created") or d.get("createdAt"),
                    "ready_at": d.get("ready"),
                    "commit_sha": meta.get("githubCommitSha") or meta.get("gitlabCommitSha"),
                    "commit_ref": meta.get("githubCommitRef"),
                    "commit_message": str(meta.get("githubCommitMessage") or "")[:200] or None,
                    "source": d.get("source"),
                }
            )
        return out
