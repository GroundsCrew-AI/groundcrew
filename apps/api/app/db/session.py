"""Async engine and session factory built from DATABASE_URL."""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

SessionFactory = async_sessionmaker[AsyncSession]


class DatabaseNotConfiguredError(RuntimeError):
    pass


def normalize_database_url(url: str) -> str:
    """Accept plain postgres URLs (as Neon prints them) and use the asyncpg driver.

    asyncpg does not understand libpq's `sslmode`, so it becomes `ssl`, and
    `channel_binding` is dropped.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query):
        if key == "sslmode":
            query.append(("ssl", value))
        elif key != "channel_binding":
            query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(normalize_database_url(url), pool_pre_ping=True)


@lru_cache
def get_engine() -> AsyncEngine:
    database_url = get_settings().database_url
    if database_url is None:
        raise DatabaseNotConfiguredError("DATABASE_URL is not set. See .env.example.")
    return create_engine(database_url.get_secret_value())


@lru_cache
def get_session_factory() -> SessionFactory:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session(
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
