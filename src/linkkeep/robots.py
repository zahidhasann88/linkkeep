"""robots.txt handling via protego, with per-origin caching.

Blocked URLs are surfaced as ``blocked_by_robots`` (info) so a report always says
*why* a page was not looked at.  Any failure to read robots.txt (404, timeout, DNS
error, malformed file) is treated as "everything allowed", which is the documented
convention and avoids false alarms from flaky hosts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from protego import Protego

from linkkeep.fetcher import Fetcher

#: Kept as a named constant so the behaviour is greppable and testable.
ROBOTS_ALLOWED_ON_ERROR = "allow"


#: Kept for third-party fakes: anything with a compatible ``fetch`` works.
FetcherLike = Fetcher


@dataclass
class RobotsDecision:
    allowed: bool
    robots_url: str
    #: ``ok``, ``not-found``, ``error``, ``disabled``, ``malformed`` or ``status-NNN``
    source: str = "ok"
    crawl_delay: float | None = None


@dataclass
class RobotsClient:
    """Caches one parsed robots.txt per origin."""

    fetcher: Fetcher
    user_agent: str
    enabled: bool = True
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    _cache: dict[str, Protego | None] = field(default_factory=dict, repr=False)
    _delays: dict[str, float | None] = field(default_factory=dict, repr=False)
    _sources: dict[str, str] = field(default_factory=dict, repr=False)

    @staticmethod
    def origin_of(url: str) -> str:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url
        return f"{parts.scheme}://{parts.netloc}"

    async def _ensure_origin(self, origin: str) -> None:
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            await self._load_origin(origin)

    async def _load_origin(self, origin: str) -> None:
        if origin in self._cache:
            return
        self._cache[origin] = None
        self._delays[origin] = None
        self._sources[origin] = "ok"
        if not self.enabled:
            self._sources[origin] = "disabled"
            return
        result = await self.fetcher.fetch(
            f"{origin}/robots.txt", method="GET", want_body=True, max_hops=2
        )
        if result.error is not None or result.status is None or result.status >= 400:
            self._sources[origin] = (
                "error" if result.error is not None else f"status-{result.status}"
            )
            return
        text = (result.body or b"").decode("utf-8", errors="replace")
        if not text.strip():
            self._sources[origin] = "empty"
            return
        try:
            parsed = Protego.parse(text)
        except Exception:  # pragma: no cover - protego is very tolerant
            self._sources[origin] = "malformed"
            return
        self._cache[origin] = parsed
        self._delays[origin] = _crawl_delay(parsed, self.user_agent)

    async def decision(self, url: str) -> RobotsDecision:
        origin = self.origin_of(url)
        await self._ensure_origin(origin)
        rules = self._cache.get(origin)
        if rules is None:
            return RobotsDecision(
                True, f"{origin}/robots.txt", self._sources.get(origin, "ok"), None
            )
        allowed = bool(rules.can_fetch(url, self.user_agent))
        return RobotsDecision(allowed, f"{origin}/robots.txt", "ok", self._delays.get(origin))

    async def allows(self, url: str) -> bool:
        return (await self.decision(url)).allowed

    async def sitemaps(self, url: str) -> list[str]:
        origin = self.origin_of(url)
        await self._ensure_origin(origin)
        rules = self._cache.get(origin)
        if rules is None:
            return []
        try:
            return [str(item) for item in rules.sitemaps]
        except Exception:  # pragma: no cover - defensive
            return []

    async def crawl_delay(self, url: str) -> float | None:
        origin = self.origin_of(url)
        await self._ensure_origin(origin)
        return self._delays.get(origin)


def _crawl_delay(rules: Protego, user_agent: str) -> float | None:
    try:
        value = rules.crawl_delay(user_agent)
    except Exception:  # pragma: no cover - defensive against protego quirks
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):  # pragma: no cover
        return None


def empty_rules() -> Protego:
    """A parsed robots.txt that allows everything."""
    return Protego.parse("User-agent: *\nDisallow:")
