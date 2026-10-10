"""Per-host rate limiting with a fake clock (no real sleeping anywhere)."""

from __future__ import annotations

import pytest

from linkkeep.ratelimit import RateLimiter


class FakeTime:
    """A clock plus an awaitable ``sleep`` that moves it forward."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


def make(rate: float, clock: FakeTime, **kwargs: object) -> RateLimiter:
    return RateLimiter(rate, clock=clock.__call__, sleep=clock.sleep, **kwargs)  # type: ignore[arg-type]


async def test_first_request_is_immediate_then_requests_are_spaced(clock: FakeTime) -> None:
    """Capacity is one request, so "5 per second" means "never closer than 200ms"."""
    limiter = make(5.0, clock)
    assert (await limiter.acquire("site.test")) == 0.0
    assert clock.sleeps == []
    assert (await limiter.acquire("site.test")) == pytest.approx(0.2)
    assert (await limiter.acquire("site.test")) == pytest.approx(0.2)
    assert clock.sleeps == [pytest.approx(0.2), pytest.approx(0.2)]
    assert pytest.approx(limiter.total_wait_seconds, abs=1e-9) == 0.4
    assert limiter.wait_count == 2


async def test_buckets_are_per_host(clock: FakeTime) -> None:
    limiter = make(2.0, clock, burst=2.0)
    assert (await limiter.acquire("a.test")) == 0.0
    assert (await limiter.acquire("a.test")) == 0.0  # burst of 2 used up
    before = clock.now
    # a different host has its own bucket, so nothing is waited for
    assert (await limiter.acquire("b.test")) == 0.0
    assert clock.now == before
    # and a.test now has to wait for a refill
    assert (await limiter.acquire("a.test")) == pytest.approx(0.5)


async def test_tokens_refill_over_time_and_are_capped(clock: FakeTime) -> None:
    limiter = make(1.0, clock)
    await limiter.acquire("a.test")
    assert limiter.state("a.test")[0] < 1e-9
    clock.now += 3.0  # three seconds pass while other hosts were busy
    tokens, _ = limiter.state("a.test")
    assert tokens == pytest.approx(1.0)  # capped at the burst size of 1
    assert (await limiter.acquire("a.test")) == 0.0
    assert limiter.state("a.test")[0] < 1e-9  # one token was all it had


async def test_burst_allows_short_spikes(clock: FakeTime) -> None:
    limiter = make(1.0, clock, burst=4.0)
    for _ in range(4):
        assert await limiter.acquire("a.test") == 0.0
    assert (await limiter.acquire("a.test")) == pytest.approx(1.0)


async def test_steady_state_matches_the_configured_rate(clock: FakeTime) -> None:
    limiter = make(4.0, clock)
    start = clock.now
    for _ in range(20):
        await limiter.acquire("a.test")
    elapsed = clock.now - start
    assert elapsed == pytest.approx(19 / 4.0, abs=1e-6)
    assert limiter.wait_count == 19


async def test_concurrent_tasks_share_one_bucket(clock: FakeTime) -> None:
    """Waiting tasks hold the host lock, so they leave one at a time."""
    import asyncio

    limiter = make(2.0, clock)
    waits = await asyncio.gather(*(limiter.acquire("a.test") for _ in range(5)))
    assert list(waits) == [pytest.approx(v) for v in (0.0, 0.5, 0.5, 0.5, 0.5)]
    assert clock.now - 100.0 == pytest.approx(2.0)
    assert limiter.state("a.test")[0] < 1e-9


async def test_zero_or_negative_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="rate must be > 0"):
        RateLimiter(0)
    with pytest.raises(ValueError, match="rate must be > 0"):
        RateLimiter(-1)


async def test_default_clock_is_monotonic() -> None:
    limiter = RateLimiter(1000.0)
    assert await limiter.acquire("whatever") == 0.0
    assert limiter.min_interval == pytest.approx(0.001)
    assert limiter.state("missing") == (1.0, 0.0)
