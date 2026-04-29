"""Broker package. Use get_broker() instead of importing implementations directly."""

from __future__ import annotations

from loguru import logger

from config.settings import Settings, settings as default_settings
from execution.broker.base import (
    AccountInfo,
    BrokerClient,
    MarketClock,
    OrderResult,
    Position,
    Side,
)


PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


def get_broker(settings: Settings | None = None) -> BrokerClient:
    """Return the broker for the current runtime mode.

    Live trading only flows when both `BROKER_MODE == "live"` and
    `ENVIRONMENT == "production"` (see Settings.is_live_trading). Any other
    combination returns a paper-mode broker pointed at the Alpaca paper URL.
    """
    s = settings or default_settings

    if s.is_live_trading:
        if s.ALPACA_BASE_URL != LIVE_URL:
            s = s.model_copy(update={"ALPACA_BASE_URL": LIVE_URL})
        from execution.broker.alpaca import AlpacaBroker

        logger.info("broker=alpaca mode=live env={env}", env=s.ENVIRONMENT)
        return AlpacaBroker(settings=s)

    if s.ALPACA_BASE_URL != PAPER_URL:
        s = s.model_copy(update={"ALPACA_BASE_URL": PAPER_URL})
    from execution.broker.alpaca import AlpacaBroker

    logger.info(
        "broker=alpaca mode=paper env={env} broker_mode={bm}",
        env=s.ENVIRONMENT,
        bm=s.BROKER_MODE,
    )
    return AlpacaBroker(settings=s)


def get_ibkr_broker(*_args, **_kwargs) -> BrokerClient:
    raise NotImplementedError("IBKR broker is a Phase 3 deliverable")


__all__ = [
    "AccountInfo",
    "BrokerClient",
    "MarketClock",
    "OrderResult",
    "Position",
    "Side",
    "get_broker",
    "get_ibkr_broker",
]
