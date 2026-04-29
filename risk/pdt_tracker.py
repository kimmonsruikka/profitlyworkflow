"""PDT (Pattern Day Trader) rolling window tracker.

Stored in Redis as a sorted set: score = unix timestamp, member = trade_id.
Window cleanup happens lazily on every read so the set never grows unbounded.
"""

from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis

from config import constants


KEY = "pdt_trades"
WINDOW_SECONDS = constants.PDT_ROLLING_DAYS * 86_400


class PdtTracker:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def increment_pdt_count(self, trade_id: str | uuid.UUID | None = None) -> int:
        """Add a day-trade to the rolling window. Returns new window count."""
        member = str(trade_id or uuid.uuid4())
        now = time.time()
        await self.redis.zadd(KEY, {member: now})
        await self._evict(now)
        return await self.redis.zcard(KEY)

    async def get_current_count(self) -> int:
        await self._evict(time.time())
        return await self.redis.zcard(KEY)

    async def get_remaining_trades(self, account_balance: float) -> int | None:
        if account_balance >= constants.PDT_ACCOUNT_THRESHOLD:
            return None
        used = await self.get_current_count()
        return max(0, constants.PDT_MAX_DAY_TRADES - used)

    async def is_pdt_restricted(self, account_balance: float) -> bool:
        if account_balance >= constants.PDT_ACCOUNT_THRESHOLD:
            return False
        used = await self.get_current_count()
        return used >= constants.PDT_MAX_DAY_TRADES

    async def _evict(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        await self.redis.zremrangebyscore(KEY, 0, cutoff)
