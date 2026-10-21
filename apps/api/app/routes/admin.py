"""Platform switches for operators (PLATFORM_ADMINS). Everyone else gets 404, as if the routes
did not exist. The same switches are on the server: `python -m app.admin_cli`."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app import platform
from app.audit import record
from app.db.models import PlatformSetting, User
from app.routes.deps import ClientIp, CurrentUser, SessionDep, SettingsDep

router = APIRouter(prefix="/admin", tags=["admin"])


class PlatformOut(BaseModel):
    signups_paused: bool
    signups_changed_at: datetime | None
    signups_last_hour: int
    signups_per_hour: int
    users: int


class SignupsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


def _admin(user: User, settings: SettingsDep) -> None:
    if not platform.is_platform_admin(user, settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


async def _out(session: SessionDep, settings: SettingsDep) -> PlatformOut:
    row = await session.get(PlatformSetting, platform.SIGNUPS_PAUSED)
    return PlatformOut(
        signups_paused=await platform.signups_paused(session),
        signups_changed_at=row.updated_at if row else None,
        signups_last_hour=await platform.signups_last_hour(session),
        signups_per_hour=settings.signups_per_hour,
        users=int(await session.scalar(select(func.count(User.id))) or 0),
    )


@router.get("/platform")
async def get_platform(
    user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> PlatformOut:
    _admin(user, settings)
    return await _out(session, settings)


@router.put("/platform/signups")
async def set_signups(
    body: SignupsIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    ip: ClientIp,
) -> PlatformOut:
    """Pause or resume new sign-ups. Invited people can still join while paused."""
    _admin(user, settings)
    await platform.set_signups_paused(session, body.paused, user.id)
    record(
        session,
        workspace_id=None,
        actor=user,
        action="platform.signups_paused" if body.paused else "platform.signups_resumed",
        ip=ip,
    )
    await session.commit()
    return await _out(session, settings)
