"""Async database session factory.

Translates DATABASE_URL to the asyncpg dialect transparently so callers can
keep using the standard postgresql:// form in .env. Also reconciles libpq's
`sslmode` query parameter (which psycopg2 understands but asyncpg rejects)
with asyncpg's `ssl=` connect argument, so the same DATABASE_URL works for
both the sync alembic path and the async runtime path.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import settings
from data.models import Base


_PLACEHOLDER_URL = "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder"

# libpq sslmode values that mean "encrypt the connection"; asyncpg
# represents these via ssl=True (with cert verification).
_SSLMODE_TLS_VALUES = {"require", "verify-ca", "verify-full"}


def _to_async_url(url: str) -> str:
    """Translate a stock DATABASE_URL to its async-driver dialect."""
    if not url:
        return _PLACEHOLDER_URL
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite+aiosqlite://"):
        return url
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _async_url_and_connect_args(url: str) -> tuple[str, dict]:
    """Return (sanitized async URL, connect_args dict).

    For postgresql+asyncpg URLs, strips the libpq-style `sslmode` query
    parameter and converts it to the asyncpg `ssl=` connect-arg form.
    Other dialects are returned untouched.
    """
    async_url = _to_async_url(url)
    connect_args: dict = {}

    if not async_url.startswith("postgresql+asyncpg://"):
        return async_url, connect_args

    parsed = urlparse(async_url)
    params = list(parse_qsl(parsed.query, keep_blank_values=True))
    kept: list[tuple[str, str]] = []
    sslmode: str | None = None
    for key, value in params:
        if key.lower() == "sslmode":
            sslmode = value.lower()
        else:
            kept.append((key, value))

    if sslmode is not None:
        if sslmode in _SSLMODE_TLS_VALUES:
            connect_args["ssl"] = True
        elif sslmode == "disable":
            connect_args["ssl"] = False
        # "allow"/"prefer" leave ssl unset — asyncpg's default is no SSL,
        # which matches "allow"; for "prefer" callers should use "require".

    new_query = urlencode(kept)
    sanitized = urlunparse(parsed._replace(query=new_query))
    return sanitized, connect_args


_async_url, _connect_args = _async_url_and_connect_args(settings.DATABASE_URL)

engine = create_async_engine(
    _async_url,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session and commit on clean exit, roll back on exception."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables from registered ORM metadata."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
