"""GitHub App webhooks (POST /github/webhook, set as the app's Webhook URL).

Every delivery must carry `X-Hub-Signature-256`, the HMAC-SHA256 of the raw body with
GITHUB_WEBHOOK_SECRET; anything else gets 401 before the body is read as JSON. Without the
secret configured the route answers 503, so an unsigned endpoint never runs.

What Groundcrew does with them (everything else is acknowledged and ignored):
- `installation` deleted: the app is gone from that account. Its GitHub connections stop with a
  clear error, and no workspace may use the installation until someone claims it again.
- `installation` suspend / unsuspend: connections stop, then work again.
- `installation_repositories` removed: those repositories leave every connection's list.
"""

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.connectors import github_claims
from app.db.models import Connection, Workspace
from app.routes.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/github", tags=["github"])
log = logging.getLogger(__name__)


class WebhookOut(BaseModel):
    handled: bool
    # What changed: "2 connections stopped", "ignored", ...
    detail: str


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def _connections(session: SessionDep, installation_id: int) -> list[Connection]:
    return list(
        await session.scalars(
            select(Connection)
            .where(
                Connection.kind == "github",
                Connection.config["installation_id"].astext == str(installation_id),
            )
            .with_for_update()
        )
    )


async def _installation(session: SessionDep, action: str, payload: dict[str, Any]) -> str:
    inst = payload.get("installation") or {}
    iid = int(inst.get("id") or 0)
    account = str((inst.get("account") or {}).get("login") or iid)
    conns = await _connections(session, iid)
    if action == "deleted":
        message = f"The Groundcrew GitHub App was uninstalled from {account}. Install it again."
        for conn in conns:
            conn.status = "error"
            conn.config = {**conn.config, "last_error": message}
        dropped = 0
        for workspace in await session.scalars(
            select(Workspace).where(Workspace.settings[github_claims.CLAIMS_KEY].has_key(str(iid)))
        ):
            dropped += int(github_claims.drop_claim(workspace, iid))
        return f"{len(conns)} connections stopped, {dropped} workspaces released it"
    if action == "suspend":
        message = f"The Groundcrew GitHub App is suspended on {account}."
        for conn in conns:
            conn.status = "error"
            conn.config = {**conn.config, "last_error": message}
        return f"{len(conns)} connections stopped"
    if action == "unsuspend":
        for conn in conns:
            conn.status = "connected"
            conn.config = {k: v for k, v in conn.config.items() if k != "last_error"}
        return f"{len(conns)} connections working again"
    return "ignored"


async def _repositories(session: SessionDep, action: str, payload: dict[str, Any]) -> str:
    if action != "removed":
        return "ignored"
    iid = int((payload.get("installation") or {}).get("id") or 0)
    gone = {str(r.get("full_name")) for r in payload.get("repositories_removed") or []}
    changed = 0
    for conn in await _connections(session, iid):
        repos = [r for r in conn.config.get("repos") or [] if r not in gone]
        if len(repos) != len(conn.config.get("repos") or []):
            conn.config = {**conn.config, "repos": repos}
            changed += 1
    return f"{changed} connections lost {len(gone)} repositories"


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> WebhookOut:
    secret = settings.github_webhook_secret
    if secret is None or not secret.get_secret_value():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhooks are not set up.")
    body = await request.body()
    if not signature_ok(secret.get_secret_value(), body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature.")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not JSON.") from None
    action = str(payload.get("action") or "")
    if x_github_event == "installation":
        detail = await _installation(session, action, payload)
    elif x_github_event == "installation_repositories":
        detail = await _repositories(session, action, payload)
    elif x_github_event == "ping":
        detail = "pong"
    else:
        detail = "ignored"
    await session.commit()
    log.info(
        "github webhook",
        extra={"event": x_github_event, "action": action, "delivery": x_github_delivery},
    )
    return WebhookOut(handled=detail not in ("ignored", "pong"), detail=detail)
