"""Async database session factory.

Translates DATABASE_URL to the asyncpg dialect transparently so callers can
keep using the standard postgresql:// form in .env.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import settings
from data.models import Base


_PLACEHOLDER_URL = "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder"


def _async_url(url: str) -> str:
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


engine = create_async_engine(_async_url(settings.DATABASE_URL), pool_pre_ping=True)

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
