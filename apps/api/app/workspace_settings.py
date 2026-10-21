"""Per-workspace settings, stored in `workspaces.settings["config"]`.

Platform-wide limits stay in environment settings (app.config); these are the choices a
workspace owner makes for their own team. Unknown keys are ignored, so older rows keep working.

The Slack webhook for notifications is a secret (anyone holding it can post to the channel),
so it is not part of the config: it is stored encrypted under its own key,
`workspaces.settings["slack_webhook"]`, and the API only says whether one is saved.
"""

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import Workspace
from app.security.crypto import DecryptionError, SecretsKeyError, decrypt, encrypt

CONFIG_KEY = "config"
SLACK_WEBHOOK_KEY = "slack_webhook"
# An incoming webhook: https://hooks.slack.com/services/T.../B.../...
SLACK_WEBHOOK = re.compile(
    r"^https://hooks\.slack\.com/services/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+$"
)

log = logging.getLogger(__name__)


class NotificationConfig(BaseModel):
    """Who is told about incidents and approvals, and which events (app.notifications)."""

    model_config = ConfigDict(extra="ignore")

    # Email to every member, to owners only, or to nobody. Only confirmed addresses get mail.
    email: Literal["members", "owners", "none"] = "owners"
    incident_opened: bool = True
    approval_needed: bool = True
    incident_resolved: bool = True
    incident_failed: bool = True


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("emergency_approvers", mode="before")
    @classmethod
    def _old_role_name(cls, value: object) -> object:
        # Saved before roles had names: "members" meant every member, who are approvers now.
        return "approvers" if value == "members" else value

    # Who may approve emergency actions (rollbacks, restarts, secret rollbacks, deleting a
    # deployment): owners only, or approvers too. Write actions (fix PRs, deploy plans) can be
    # approved by owners and approvers; viewers never approve.
    emergency_approvers: Literal["owners", "approvers"] = "owners"
    # Let the incident watcher open incidents for this workspace (needs the platform's
    # INCIDENT_WATCH_ENABLED too). Off: incidents are only opened by a person.
    incident_watch: bool = True
    # This workspace's model spend per UTC day, in USD. Null: the platform's LLM_DAILY_USD_CAP.
    daily_usd_cap: float | None = Field(default=None, ge=0, le=10_000)
    # Incident and approval notifications. Left out of an update: the saved ones are kept.
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)


def get_config(workspace: Workspace) -> WorkspaceConfig:
    raw: Any = (workspace.settings or {}).get(CONFIG_KEY) or {}
    return WorkspaceConfig.model_validate(raw if isinstance(raw, dict) else {})


def set_config(workspace: Workspace, config: WorkspaceConfig) -> None:
    """The caller commits. Other keys in workspaces.settings (system summary) are kept."""
    workspace.settings = {**(workspace.settings or {}), CONFIG_KEY: config.model_dump()}


def slack_saved(workspace: Workspace) -> bool:
    return bool((workspace.settings or {}).get(SLACK_WEBHOOK_KEY))


def slack_webhook(workspace: Workspace) -> str | None:
    """The decrypted webhook URL, or None when none is saved or it cannot be decrypted (a
    rotated SECRETS_KEY: the owner saves it again)."""
    token = (workspace.settings or {}).get(SLACK_WEBHOOK_KEY)
    if not isinstance(token, str) or not token:
        return None
    try:
        return decrypt(token)
    except (DecryptionError, SecretsKeyError):
        log.warning("slack webhook of workspace %s cannot be decrypted", workspace.id)
        return None


def set_slack_webhook(workspace: Workspace, url: str | None) -> None:
    """Save the webhook encrypted, or remove it (None). The caller checks the URL against
    SLACK_WEBHOOK and commits. Raises SecretsKeyError when SECRETS_KEY is not set."""
    settings = dict(workspace.settings or {})
    if url:
        settings[SLACK_WEBHOOK_KEY] = encrypt(url)
    else:
        settings.pop(SLACK_WEBHOOK_KEY, None)
    workspace.settings = settings
