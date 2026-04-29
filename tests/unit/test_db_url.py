from __future__ import annotations

from data.db import _async_url_and_connect_args


def test_postgresql_with_sslmode_require_converts_to_ssl_true() -> None:
    """libpq's sslmode=require → asyncpg's ssl=True; query param removed."""
    url = "postgresql://user:pw@host:5432/db?sslmode=require"
    result, args = _async_url_and_connect_args(url)
    assert result.startswith("postgresql+asyncpg://")
    assert "sslmode" not in result
    assert args == {"ssl": True}


def test_postgresql_with_verify_full_also_maps_to_ssl_true() -> None:
    url = "postgresql://u:p@host/db?sslmode=verify-full"
    result, args = _async_url_and_connect_args(url)
    assert "sslmode" not in result
    assert args == {"ssl": True}


def test_sslmode_disable_maps_to_ssl_false() -> None:
    url = "postgresql://u:p@host/db?sslmode=disable"
    _, args = _async_url_and_connect_args(url)
    assert args == {"ssl": False}


def test_postgresql_without_sslmode_returns_empty_connect_args() -> None:
    url = "postgresql://user:pw@host:5432/db"
    result, args = _async_url_and_connect_args(url)
    assert result == "postgresql+asyncpg://user:pw@host:5432/db"
    assert args == {}


def test_other_query_params_are_preserved() -> None:
    """sslmode goes; other libpq params (if any) survive the rewrite."""
    url = "postgresql://u:p@host/db?application_name=watcher&sslmode=require"
    result, args = _async_url_and_connect_args(url)
    assert "application_name=watcher" in result
    assert "sslmode" not in result
    assert args == {"ssl": True}


def test_already_asyncpg_url_with_sslmode_still_normalized() -> None:
    """Hand-written postgresql+asyncpg:// URLs with sslmode are also fixed."""
    url = "postgresql+asyncpg://u:p@host/db?sslmode=require"
    result, args = _async_url_and_connect_args(url)
    assert "sslmode" not in result
    assert args == {"ssl": True}


def test_sqlite_url_translates_to_aiosqlite_no_ssl() -> None:
    url = "sqlite:///./test.db"
    result, args = _async_url_and_connect_args(url)
    assert result == "sqlite+aiosqlite:///./test.db"
    assert args == {}


def test_empty_url_uses_placeholder() -> None:
    result, args = _async_url_and_connect_args("")
    assert result.startswith("postgresql+asyncpg://placeholder")
    assert args == {}
