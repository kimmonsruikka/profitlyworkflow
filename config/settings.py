"""Application settings loaded from environment variables.

Import the singleton instead of constructing the class:

    from config.settings import settings
"""

from __future__ import annotations

import sys
from typing import Literal

from loguru import logger
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Runtime
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    BROKER_MODE: Literal["paper", "live"] = "paper"

    # Broker — Alpaca
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"

    # Broker — IBKR
    IBKR_HOST: str = "127.0.0.1"
    IBKR_PORT: int = 7497
    IBKR_CLIENT_ID: int = 1

    # Database
    DATABASE_URL: str = ""
    REDIS_URL: str = "redis://localhost:6379/0"

    # Market Data
    POLYGON_API_KEY: str = ""
    BENZINGA_API_KEY: str = ""
    ORTEX_API_KEY: str = ""

    # SEC / EDGAR
    SEC_API_KEY: str = ""

    # Telegram alert bot
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Telegram pump-group monitoring (Telethon)
    TELEGRAM_MONITOR_API_ID: str = ""
    TELEGRAM_MONITOR_API_HASH: str = ""

    # Reddit
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USER_AGENT: str = ""

    # X / StockTwits
    X_API_BEARER_TOKEN: str = ""
    STOCKTWITS_ACCESS_TOKEN: str = ""

    # Prefect
    PREFECT_API_KEY: str = ""
    PREFECT_API_URL: str = ""

    # Application
    LOG_LEVEL: str = Field(default="INFO")
    TIMEZONE: str = "America/New_York"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live_trading(self) -> bool:
        """Live orders only flow when both gates are explicitly set."""
        return self.BROKER_MODE == "live" and self.ENVIRONMENT == "production"


settings = Settings()


def _configure_logger() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )


_configure_logger()
