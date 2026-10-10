"""Shared fixtures and fakes.

``FakeFetcher`` is the workhorse: it implements the crawler's HTTP interface from a
route table, so crawl behaviour can be tested without any sockets at all.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from linkkeep.config import Config
from linkkeep.fetcher import Fetcher, FetchError, FetchResult
from linkkeep.models import Issue, IssueType, RedirectHop, make_fingerprint
from linkkeep.storage import Storage

BASE = "https://site.test"
NOW = dt.datetime(2026, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc)


class FakeFetcher(Fetcher):
    """A :class:`Fetcher` backed by a dict of routes.

    Each route is a dict with any of: ``status``, ``html``, ``body``, ``content_type``,
    ``location`` (a redirect), ``chain`` (a list of ``(url, status)`` hops),
    ``error`` (``(kind, detail)``), ``duration_ms``, ``head_status`` (what HEAD gets when
    it is rejected), ``delay``.
    """

    def __init__(
        self,
        routes: Mapping[str, dict[str, Any]] | None = None,
        *,
        default_status: int | None = 404,
        allow_unknown: bool = False,
    ) -> None:
        self.routes: dict[str, dict[str, Any]] = dict(routes or {})
        self.default_status = default_status
        self.allow_unknown = allow_unknown
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    # -- helpers ---------------------------------------------------------------
    def add(self, url: str, **route: Any) -> None:
        self.routes[url] = route

    def urls_seen(self, *, method: str | None = None) -> list[str]:
        return [url for url, kind in self.calls if method is None or kind == method]

    def count_for(self, url: str) -> int:
        return sum(1 for seen, _ in self.calls if seen == url)

    def _resolve(self, url: str) -> dict[str, Any] | None:
        if url in self.routes:
            return self.routes[url]
        stripped = url.split("#", 1)[0]
        if stripped != url:
            return self.routes.get(stripped)
        without_slash = stripped.rstrip("/") or "/"
        return self.routes.get(without_slash)

    # -- Fetcher interface -----------------------------------------------------
    async def fetch(
        self, url: str, *, method: str = "GET", want_body: bool = False, max_hops: int | None = None
    ) -> FetchResult:
        """Resolve ``url``, following ``location`` hops the same way the real fetcher does."""
        self.calls.append((url, method))
        limit = 10 if max_hops is None else int(max_hops)
        current = url
        chain: list[RedirectHop] = []
        seen: set[str] = {url}
        status: int | None = None
        looped = False
        for _hop in range(limit + 1):
            route = self._resolve(current)
            if route is None:
                if self.default_status is None and not self.allow_unknown:
                    error = FetchError("dns", f"Name or service not known: {current}")
                    return FetchResult(
                        url=url,
                        method=method,
                        status=None,
                        final_url=current,
                        chain=chain,
                        error=error,
                    )
                status = self.default_status
                chain.append(RedirectHop(url=current, status=status))
                return self._final(url, method, status, current, chain, None, want_body)
            if route.get("error") is not None:
                kind, detail = (
                    route["error"] if isinstance(route["error"], tuple) else (route["error"], "")
                )
                return FetchResult(
                    url=url,
                    method=method,
                    status=None,
                    final_url=current,
                    chain=chain,
                    error=FetchError(kind, detail),
                )
            status = (
                route.get("head_status", route.get("status", 200))
                if method == "HEAD"
                else route.get("status", 200)
            )
            location = route.get("location")
            chain.append(RedirectHop(url=current, status=status, location=location))
            if not location:
                return self._final(url, method, status, current, chain, route, want_body)
            nxt = location if "://" in location else f"{BASE}{location}"
            if nxt in seen or nxt == current:
                looped = True
                break
            seen.add(nxt)
            self.calls.append((nxt, method))
            current = nxt
        if looped:
            error = FetchError("redirect_loop", f"redirect loop back to {current}")
        else:
            error = FetchError(
                "redirect_limit", f"more than {limit} redirects ({len(chain)} hops recorded)"
            )
        return FetchResult(
            url=url,
            method=method,
            status=status,
            final_url=current,
            chain=chain,
            error=error,
            duration_ms=len(chain),
        )

    def _final(
        self,
        url: str,
        method: str,
        status: int | None,
        current: str,
        chain: list[RedirectHop],
        spec: dict[str, Any] | None,
        want_body: bool,
        *,
        route: Any = None,
    ) -> FetchResult:
        del route  # kept for call-site readability
        spec = spec or {}
        body: bytes | None = None
        if want_body and method != "HEAD":
            raw = spec.get("html", spec.get("body"))
            if raw is not None:
                body = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        duration = int(spec.get("duration_ms", 2))
        return FetchResult(
            url=url,
            method=method,
            status=status,
            final_url=current,
            chain=chain,
            body=body,
            content_type=spec.get("content_type", "text/html; charset=utf-8" if body else None),
            size_bytes=len(body) if body is not None else None,
            truncated=bool(spec.get("truncated")),
            duration_ms=duration,
        )

    async def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------------------ config
def make_config(**overrides: Any) -> Config:
    """Config with test-friendly defaults (fast, no robots surprises)."""
    base: dict[str, Any] = {
        "rules": {"allow_private": True},
        "output": {"color": False},
    }
    merged = _deep_merge(base, overrides)
    return Config.model_validate(merged)


def _deep_merge(a: dict[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for key, value in b.items():
        if isinstance(out.get(key), dict) and isinstance(value, Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "linkkeep.sqlite")
    yield store
    store.close()


def page(html: str, **extra: Any) -> dict[str, Any]:
    return {"status": 200, "html": html, "content_type": "text/html; charset=utf-8", **extra}


def issue(
    code: IssueType | str,
    *,
    source: str | None = f"{BASE}/",
    target: str = f"{BASE}/broken",
    status: int | None = None,
    reason: str | None = None,
    final: str | None = None,
    element: str | None = "a",
    attribute: str | None = "href",
    link_text: str | None = None,
    chain: list[RedirectHop] | None = None,
    found_at: dt.datetime = NOW,
) -> Issue:
    """Build an issue with a real fingerprint (fingerprints must match production)."""
    kind = IssueType(code) if not isinstance(code, IssueType) else code
    return Issue(
        type=kind,
        fingerprint=make_fingerprint(kind, source, target),
        source_url=source,
        target_url=target,
        final_url=final,
        status=status,
        reason=reason,
        element=element,
        attribute=attribute,
        link_text=link_text,
        redirect_chain=chain or [],
        found_at=found_at,
    )


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    return FakeFetcher(allow_unknown=True, default_status=200)
