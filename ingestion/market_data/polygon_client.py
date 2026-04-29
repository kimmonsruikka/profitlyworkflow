"""Async wrapper around polygon-api-client.

The underlying SDK is sync. We wrap each call in asyncio.to_thread() and
gate every call through a token-bucket-style throttler so the Starter
tier's 5 req/min quota is never exceeded. When you upgrade to real-time
just bump POLYGON_REQUESTS_PER_MINUTE in config/constants.py.

Methods return plain dicts so callers don't depend on the SDK's response
classes (which can drift between SDK minor versions).
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any

from loguru import logger

from config import constants
from config.settings import settings


class PolygonNotFoundError(Exception):
    """Polygon returned 404 / "ticker does not exist"."""


def _is_not_found(exc: Exception) -> bool:
    """Detect 404-equivalent errors from polygon-api-client."""
    text = repr(exc).lower()
    return "404" in text or "not found" in text or "no results" in text


class PolygonClient:
    def __init__(
        self,
        api_key: str | None = None,
        requests_per_minute: int | None = None,
    ) -> None:
        from polygon import RESTClient

        self._client = RESTClient(api_key or settings.POLYGON_API_KEY)
        rpm = requests_per_minute or constants.POLYGON_REQUESTS_PER_MINUTE
        # 1.05x buffer keeps us under the documented limit even with clock drift.
        self._min_interval = (60.0 / rpm) * 1.05
        self._last_request_at: float = 0.0
        self._lock = asyncio.Lock()
        logger.info(
            "PolygonClient initialized (rpm={rpm}, interval={iv:.2f}s)",
            rpm=rpm, iv=self._min_interval,
        )

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def _call(self, fn, *args, **kwargs):
        await self._throttle()
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            if _is_not_found(exc):
                raise PolygonNotFoundError(str(exc)) from exc
            logger.exception("polygon call {} failed", fn.__name__)
            raise

    # -------------------- ticker reference --------------------
    async def get_ticker_details(self, ticker: str) -> dict[str, Any]:
        details = await self._call(self._client.get_ticker_details, ticker)
        # share_class_shares_outstanding is the closest free-trading proxy
        # Polygon exposes for true float; refine with Ortex later.
        return {
            "ticker": getattr(details, "ticker", ticker),
            "name": getattr(details, "name", None),
            "exchange": getattr(details, "primary_exchange", None),
            "sector": getattr(details, "sic_description", None),
            "float_shares": getattr(details, "share_class_shares_outstanding", None),
            "shares_outstanding": getattr(details, "weighted_shares_outstanding", None),
            "market_cap": getattr(details, "market_cap", None),
        }

    # -------------------- pricing --------------------
    async def get_previous_close(self, ticker: str) -> dict[str, Any]:
        result = await self._call(self._client.get_previous_close_agg, ticker)
        # SDK returns a list-like of one Agg object
        agg = result[0] if isinstance(result, list) and result else result
        return {
            "ticker": ticker,
            "open": getattr(agg, "open", None),
            "high": getattr(agg, "high", None),
            "low": getattr(agg, "low", None),
            "close": getattr(agg, "close", None),
            "volume": getattr(agg, "volume", None),
            "vwap": getattr(agg, "vwap", None),
            "timestamp": getattr(agg, "timestamp", None),
        }

    async def get_snapshot(self, ticker: str) -> dict[str, Any]:
        snap = await self._call(self._client.get_snapshot_ticker, "stocks", ticker)
        last_quote = getattr(snap, "last_quote", None)
        last_trade = getattr(snap, "last_trade", None)
        day = getattr(snap, "day", None)
        bid = getattr(last_quote, "bid", None) if last_quote else None
        ask = getattr(last_quote, "ask", None) if last_quote else None
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        return {
            "ticker": ticker,
            "price": getattr(last_trade, "price", None) if last_trade else None,
            "volume": getattr(day, "volume", None) if day else None,
            "vwap": getattr(day, "vwap", None) if day else None,
            "bid": bid,
            "ask": ask,
            "spread": spread,
        }

    async def get_daily_bars(
        self, ticker: str, from_date: date, to_date: date
    ) -> list[dict[str, Any]]:
        return await self._aggs(ticker, 1, "day", from_date, to_date)

    async def get_minute_bars(
        self, ticker: str, from_date: date, to_date: date
    ) -> list[dict[str, Any]]:
        return await self._aggs(ticker, 1, "minute", from_date, to_date)

    async def _aggs(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        bars = await self._call(
            self._client.get_aggs,
            ticker,
            multiplier,
            timespan,
            from_date.isoformat(),
            to_date.isoformat(),
        )
        return [
            {
                "ticker": ticker,
                "open": getattr(b, "open", None),
                "high": getattr(b, "high", None),
                "low": getattr(b, "low", None),
                "close": getattr(b, "close", None),
                "volume": getattr(b, "volume", None),
                "vwap": getattr(b, "vwap", None),
                "timestamp": getattr(b, "timestamp", None),
            }
            for b in (bars or [])
        ]
