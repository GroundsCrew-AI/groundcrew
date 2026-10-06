"""Shared route dependencies."""

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import SessionFactory, get_session, get_session_factory

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def require_dev_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """TODO Phase 2: replace with real auth (Auth.js + GitHub OAuth).

    Until then, setting DEV_API_KEY makes every route except /health require it in the
    X-API-Key header. Unset (the default) means no check.
    """
    expected = settings.dev_api_key
    if expected is None:
        return
    if x_api_key is None or not secrets.compare_digest(
        x_api_key.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or wrong X-API-Key.")
