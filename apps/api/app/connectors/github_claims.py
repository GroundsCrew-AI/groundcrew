"""Which GitHub App installations a workspace may use.

Groundcrew's GitHub App is installed on many accounts. A workspace may list and link only the
installations its owner proved access to on GitHub: the owner authorizes the app (OAuth, the
same app's client), GitHub returns a short-lived user token for that one exchange, and
`GET /user/installations` with it lists exactly the installations of this app that person can
reach on GitHub. The installation ids are kept in `workspaces.settings["github_installations"]`
and the user token is dropped at once.

The OAuth `state` is a 10-minute token signed with INTERNAL_JWT_SECRET, bound to the user and
the workspace, so a callback cannot be replayed into another workspace or by another person.

The only calls here: the OAuth code exchange (POST github.com/login/oauth/access_token) and
GET /user/installations. Everything else goes through app.connectors.github (GET only).
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import Settings
from app.connectors.github import API_VERSION, GitHubError
from app.db.models import Connection, Workspace
from app.security.redact import redact

CLAIMS_KEY = "github_installations"
STATE_AUDIENCE = "groundcrew-github-claim"
STATE_SECONDS = 600
OAUTH_URL = "https://github.com/login/oauth"
CALLBACK_PATH = "/connections/github/callback"


def callback_url(settings: Settings) -> str:
    return settings.web_origin.rstrip("/") + CALLBACK_PATH


def _secret(settings: Settings) -> str:
    secret = settings.internal_jwt_secret
    if secret is None or not secret.get_secret_value():
        raise GitHubError("INTERNAL_JWT_SECRET is not set.")
    return secret.get_secret_value()


def make_state(settings: Settings, user_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "aud": STATE_AUDIENCE,
            "sub": str(user_id),
            "ws": str(workspace_id),
            "iat": now,
            "exp": now + STATE_SECONDS,
        },
        _secret(settings),
        algorithm="HS256",
    )


def read_state(settings: Settings, state: str, user_id: uuid.UUID) -> uuid.UUID:
    """The workspace the authorization was started for, if `state` is valid and this user's."""
    try:
        claims = jwt.decode(state, _secret(settings), algorithms=["HS256"], audience=STATE_AUDIENCE)
    except jwt.PyJWTError:
        raise GitHubError(
            "This GitHub authorization expired or is not valid. Start again."
        ) from None
    if claims.get("sub") != str(user_id):
        raise GitHubError("This GitHub authorization was started by someone else. Start again.")
    return uuid.UUID(str(claims["ws"]))


def authorize_url(settings: Settings, state: str) -> str:
    if not settings.github_app_client_id:
        raise GitHubError("GITHUB_APP_CLIENT_ID is not set.")
    query = urlencode(
        {
            "client_id": settings.github_app_client_id,
            "redirect_uri": callback_url(settings),
            "state": state,
            "allow_signup": "false",
        }
    )
    return f"{OAUTH_URL}/authorize?{query}"


async def user_installations(
    settings: Settings, code: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, Any]]:
    """Exchange the OAuth code and list this app's installations the user can reach. The user
    token is used for that one call and never kept."""
    secret = settings.github_app_client_secret
    if not settings.github_app_client_id or secret is None:
        raise GitHubError("GITHUB_APP_CLIENT_ID and GITHUB_APP_CLIENT_SECRET must be set.")
    async with httpx.AsyncClient(transport=transport, timeout=20) as client:
        try:
            exchanged = await client.post(
                f"{OAUTH_URL}/access_token",
                data={
                    "client_id": settings.github_app_client_id,
                    "client_secret": secret.get_secret_value(),
                    "code": code,
                    "redirect_uri": callback_url(settings),
                },
                headers={"Accept": "application/json", "User-Agent": "groundcrew"},
            )
            token = str((exchanged.json() or {}).get("access_token") or "")
            if exchanged.status_code >= 400 or not token:
                raise GitHubError("GitHub did not accept the authorization. Start again.")
            found: list[dict[str, Any]] = []
            page = 1
            while True:
                r = await client.get(
                    f"{settings.github_api_url}/user/installations",
                    params={"per_page": 100, "page": page},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "groundcrew",
                    },
                )
                if r.status_code >= 400:
                    raise GitHubError(f"GitHub /user/installations returned {r.status_code}.")
                batch = r.json().get("installations") or []
                found += [
                    {"id": int(i["id"]), "account": str(i["account"]["login"])} for i in batch
                ]
                if len(batch) < 100:
                    return found
                page += 1
        except httpx.HTTPError as exc:
            raise GitHubError(redact(f"GitHub sign-in check: {type(exc).__name__}")) from None
        except (ValueError, KeyError, TypeError):
            raise GitHubError("GitHub answered with something unexpected. Start again.") from None


def claimed(workspace: Workspace, connections: list[Connection]) -> dict[int, dict[str, Any]]:
    """Installations this workspace may use: those its owners proved access to, plus those
    its existing GitHub connections already use."""
    stored = (workspace.settings or {}).get(CLAIMS_KEY) or {}
    out = {int(k): dict(v) for k, v in stored.items() if str(k).isdigit()}
    for conn in connections:
        iid = (conn.config or {}).get("installation_id")
        if conn.kind == "github" and iid is not None:
            out.setdefault(int(iid), {"account": (conn.config or {}).get("account")})
    return out


def add_claims(workspace: Workspace, found: list[dict[str, Any]], user_id: uuid.UUID) -> list[int]:
    stored = dict((workspace.settings or {}).get(CLAIMS_KEY) or {})
    now = datetime.now(UTC).isoformat()
    for item in found:
        stored[str(item["id"])] = {
            "account": item["account"],
            "claimed_by": str(user_id),
            "claimed_at": now,
        }
    workspace.settings = {**(workspace.settings or {}), CLAIMS_KEY: stored}
    return [int(i["id"]) for i in found]


def drop_claim(workspace: Workspace, installation_id: int) -> bool:
    stored = dict((workspace.settings or {}).get(CLAIMS_KEY) or {})
    if stored.pop(str(installation_id), None) is None:
        return False
    workspace.settings = {**(workspace.settings or {}), CLAIMS_KEY: stored}
    return True
