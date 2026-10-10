"""Per-host token bucket rate limiting with an injectable clock (for tests)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def __call__(self) -> float: ...


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Allow ``rate`` requests per second, per key (host).

    ``burst`` defaults to one request's worth of capacity, which is what "N per second"
    means in practice; raise it only if you know you want short spikes.
    """

    def __init__(
        self,
        rate: float,
        *,
        burst: float | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if rate <= 0:
            msg = "rate must be > 0"
            raise ValueError(msg)
        self.rate = float(rate)
        self.capacity = float(burst) if burst is not None else 1.0
        self._clock: Clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._buckets: dict[str, _Bucket] = {}
        self._rates: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.total_wait_seconds = 0.0
        self.wait_count = 0

    @property
    def min_interval(self) -> float:
        return 1.0 / self.rate

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def set_delay(self, key: str, delay: float) -> None:
        if delay > 0:
            self._rates[key] = min(self.rate, 1.0 / delay)

    def _refill(self, bucket: _Bucket, now: float, rate: float | None = None) -> _Bucket:
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(
            self.capacity, bucket.tokens + elapsed * (rate if rate is not None else self.rate)
        )
        bucket.updated = now
        return bucket

    async def acquire(self, key: str) -> float:
        """Wait until a request to ``key`` may go out; return seconds waited."""
        lock = await self._lock_for(key)
        waited = 0.0
        async with lock:
            while True:
                now = self._clock()
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _Bucket(tokens=self.capacity, updated=now)
                    self._buckets[key] = bucket
                rate = self._rates.get(key, self.rate)
                self._refill(bucket, now, rate)
                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return waited
                needed = 1.0 - bucket.tokens
                delay = needed / rate
                await self._sleep(delay)
                waited += delay
                self.total_wait_seconds += delay
                self.wait_count += 1

    def state(self, key: str) -> tuple[float, float]:
        """``(tokens, last_update)`` after refilling - exposed for tests and ``--verbose``."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return self.capacity, 0.0
        self._refill(bucket, self._clock())
        return bucket.tokens, bucket.updated
