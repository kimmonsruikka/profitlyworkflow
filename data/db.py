"""Async database session factory.

Translates DATABASE_URL to the asyncpg dialect transparently so callers can
keep using the standard postgresql:// form in .env. Also reconciles libpq's
`sslmode` query parameter (which psycopg2 understands but asyncpg rejects)
with asyncpg's `ssl=` connect argument, so the same DATABASE_URL works for
both the sync alembic path and the async runtime path.
"""

from __future__ import annotations

import ssl
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

# sslmode values that mean "encrypt the connection but don't verify the
# server's certificate" — equivalent to libpq sslmode=require. Used by
# DigitalOcean Managed Postgres (self-signed CA) and similar setups.
_SSLMODE_UNVERIFIED = {"require"}

# sslmode values that mean "encrypt AND verify the server cert against the
# system CA bundle" — these map to asyncpg's ssl=True (default-context).
_SSLMODE_VERIFIED = {"verify-ca", "verify-full"}


def _unverified_ssl_context() -> ssl.SSLContext:
    """SSLContext that encrypts but doesn't verify the peer cert.

    Equivalent to psycopg2's sslmode=require behavior. Required when
    connecting to a Postgres instance with a self-signed CA (e.g. the
    DigitalOcean Managed Postgres CA bundle isn't in the system trust
    store).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


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
        if sslmode in _SSLMODE_UNVERIFIED:
            connect_args["ssl"] = _unverified_ssl_context()
        elif sslmode in _SSLMODE_VERIFIED:
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


@asynccontextmanager
async def session_from(
    factory: async_sessionmaker,
) -> AsyncIterator[AsyncSession]:
    """Yield a session from `factory`, commit on clean exit, roll back on error.

    Same semantics as `get_session()` but uses a caller-supplied
    sessionmaker instead of the module-level one. Pair with
    `task_local_session_factory()` for Celery tasks.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def task_local_session_factory() -> AsyncIterator[async_sessionmaker]:
    """Per-task engine + sessionmaker. Use from Celery tasks.

    Background: Celery's prefork worker invokes tasks via
    ``asyncio.run(...)``, which creates a fresh event loop per task.
    The module-level ``engine`` / ``AsyncSessionLocal`` get bound to
    whichever loop calls into them first; reusing them in subsequent
    asyncio.run() loops produces

        RuntimeError: got Future attached to a different loop

    inside asyncpg. The fix is to create the engine on the task's own
    loop and dispose it on the way out. Yielded value is a
    ``async_sessionmaker`` so the task body can open as many sessions
    as it needs (filing persistence + signal eval, etc.) — they all
    share the task-local engine.

    Pattern:

        async def my_task_body():
            async with task_local_session_factory() as factory:
                async with factory() as session:
                    ...   # filing persistence
                async with factory() as session:
                    ...   # signal eval
            # engine disposed here

    Disposal cost is one TCP teardown per task; acceptable on the
    Starter-tier Postgres. If task volume grows enough that disposal
    is the bottleneck, switch to a per-loop engine cache keyed on
    ``id(asyncio.get_running_loop())`` — same correctness shape,
    amortizes engine setup across tasks that share a loop.
    """
    async_url, connect_args = _async_url_and_connect_args(settings.DATABASE_URL)
    task_engine = create_async_engine(
        async_url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    try:
        factory = async_sessionmaker(
            bind=task_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        yield factory
    finally:
        await task_engine.dispose()
