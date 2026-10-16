"""Per-workspace settings, stored in `workspaces.settings["config"]`.

Platform-wide limits stay in environment settings (app.config); these are the choices a
workspace owner makes for their own team. Unknown keys are ignored, so older rows keep working.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import Workspace

CONFIG_KEY = "config"


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Who may approve emergency actions (rollbacks, restarts, secret rollbacks, deleting a
    # deployment). Write actions (fix PRs, deploy plans) can be approved by any member.
    emergency_approvers: Literal["owners", "members"] = "owners"
    # Let the incident watcher open incidents for this workspace (needs the platform's
    # INCIDENT_WATCH_ENABLED too). Off: incidents are only opened by a person.
    incident_watch: bool = True
    # This workspace's model spend per UTC day, in USD. Null: the platform's LLM_DAILY_USD_CAP.
    daily_usd_cap: float | None = Field(default=None, ge=0, le=10_000)


def get_config(workspace: Workspace) -> WorkspaceConfig:
    raw: Any = (workspace.settings or {}).get(CONFIG_KEY) or {}
    return WorkspaceConfig.model_validate(raw if isinstance(raw, dict) else {})


def set_config(workspace: Workspace, config: WorkspaceConfig) -> None:
    """The caller commits. Other keys in workspaces.settings (system summary) are kept."""
    workspace.settings = {**(workspace.settings or {}), CONFIG_KEY: config.model_dump()}
