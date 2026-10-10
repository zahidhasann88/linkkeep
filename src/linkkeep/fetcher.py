"""HTTP layer: an httpx wrapper that follows redirects *manually*.

Redirects are followed hop by hop so the whole chain can be recorded, which makes
loops and over-long chains detectable facts rather than an httpx exception.

The crawler never talks to httpx directly: it receives a :class:`Fetcher`.  Tests
inject a fake, which keeps the crawl engine testable without sockets.
"""

from __future__ import annotations

import abc
import asyncio
import os
import ssl
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin

import httpx

from linkkeep.models import RedirectHop
from linkkeep.ratelimit import RateLimiter

Method = Literal["GET", "HEAD"]

REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 305, 307, 308})
#: Statuses that mean "HEAD is not welcome here", so we retry the check with GET.
HEAD_REJECTED_STATUSES: frozenset[int] = frozenset(
    {400, 401, 403, 405, 406, 415, 500, 501, 502, 503, 505}
)
RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

ERROR_DNS = "dns"
ERROR_CONNECT = "connect"
ERROR_TLS = "tls"
ERROR_TIMEOUT = "timeout"
ERROR_LOOP = "redirect_loop"
ERROR_TOO_MANY_HOPS = "redirect_limit"
ERROR_BAD_LOCATION = "redirect_without_location"
ERROR_TOO_LARGE = "too_large"
ERROR_OTHER = "error"

#: Errors worth a second attempt (DNS failures and loops are deliberately not retried).
TRANSIENT_ERRORS: frozenset[str] = frozenset({ERROR_CONNECT, ERROR_TIMEOUT, ERROR_TLS, ERROR_OTHER})

SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key", "x-auth-token"}
)


@dataclass(frozen=True)
class FetchError:
    """Why a request never produced a usable response."""

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass
class _Timing:
    """Per-call accumulator so that concurrent requests never share a mutable fetcher field."""

    network: float = 0.0
    waited: float = 0.0


@dataclass
class FetchResult:
    """Outcome of one logical URL check (after redirects and retries)."""

    url: str
    method: Method = "GET"
    status: int | None = None
    final_url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    duration_ms: int = 0
    #: Time spent waiting for our own rate limiter. Excluded from ``duration_ms`` so that
    #: a "slow" finding always means *the server* was slow, never that we were polite.
    waited_ms: int = 0
    chain: list[RedirectHop] = field(default_factory=list)
    error: FetchError | None = None
    body: bytes | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    truncated: bool = False
    attempts: int = 1
    head_fell_back: bool = False

    def __post_init__(self) -> None:
        if not self.final_url:
            self.final_url = self.url

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400

    @property
    def is_error_status(self) -> bool:
        return self.status is not None and self.status >= 400

    @property
    def redirect_count(self) -> int:
        return sum(1 for hop in self.chain if hop.status in REDIRECT_STATUSES)

    @property
    def first_redirect_status(self) -> int | None:
        for hop in self.chain:
            if hop.status in REDIRECT_STATUSES:
                return hop.status
        return None

    @property
    def is_html(self) -> bool:
        ctype = (self.content_type or "").lower()
        if "text/html" in ctype or "application/xhtml" in ctype:
            return True
        if not ctype and self.body is not None:
            return b"<html" in self.body[:2048].lower() or b"<!doctype" in self.body[:2048].lower()
        return False

    @property
    def html_body(self) -> str | None:
        """Decoded body when the response looks like HTML and fit in the parse limit."""
        if self.body is None or not self.is_html:
            return None
        return self.body.decode("utf-8", errors="replace")

    @property
    def error_reason(self) -> str:
        return "" if self.error is None else self.error.detail


def classify_exception(exc: BaseException) -> FetchError:
    """Map an httpx/OS exception onto one of our stable error kinds."""
    text = f"{type(exc).__name__}: {exc}".strip()
    lowered = text.lower()
    if isinstance(exc, httpx.TimeoutException):
        return FetchError(ERROR_TIMEOUT, text or "request timed out")
    if isinstance(exc, httpx.NetworkError):
        inner = exc.__cause__
        if isinstance(inner, ssl.SSLError) or "ssl" in lowered or "certificate" in lowered:
            return FetchError(ERROR_TLS, text)
        if _is_dns_failure(lowered):
            return FetchError(ERROR_DNS, text)
        return FetchError(ERROR_CONNECT, text)
    if isinstance(exc, httpx.TooManyRedirects):
        return FetchError(ERROR_TOO_MANY_HOPS, text)
    if isinstance(exc, (httpx.InvalidURL, ValueError)):
        return FetchError(ERROR_OTHER, text)
    return FetchError(ERROR_OTHER, text)


def _is_dns_failure(lowered_text: str) -> bool:
    return any(
        marker in lowered_text
        for marker in (
            "getaddrinfo",
            "nodename nor servname",
            "name or service not known",
            "name does not resolve",
            "temporary failure in name resolution",
        )
    )


class Fetcher(abc.ABC):
    """The only HTTP interface the crawler uses."""

    @abc.abstractmethod
    async def fetch(
        self,
        url: str,
        *,
        method: Method = "GET",
        want_body: bool = False,
        max_hops: int | None = None,
    ) -> FetchResult:
        """Fetch ``url``, following redirects manually; never raises for network errors."""

    async def check_link(self, url: str, *, prefer_head: bool = True) -> FetchResult:
        """Status-only check for a link: HEAD first, GET when HEAD is rejected."""
        if not prefer_head:
            return await self.fetch(url, method="GET")
        result = await self.fetch(url, method="HEAD")
        if result.error is None and result.status not in HEAD_REJECTED_STATUSES:
            return result
        fallback = await self.fetch(url, method="GET")
        fallback.head_fell_back = True
        return fallback

    async def close(self) -> None:  # pragma: no cover - trivial default
        return None

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


class HttpxFetcher(Fetcher):
    """Real-network implementation built on ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        rate: float = 5.0,
        retries: int = 1,
        max_hops: int = 3,
        max_bytes: int = 2_000_000,
        user_agent: str = "linkkeep/dev",
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        limiter: RateLimiter | None = None,
        verify: bool = True,
        retry_statuses: Iterable[int] = RETRY_STATUSES,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_hops = max_hops
        self.max_bytes = max_bytes
        self.retries = max(0, int(retries))
        self.retry_statuses = frozenset(retry_statuses)
        self._clock = clock
        self._limiter = limiter if limiter is not None else RateLimiter(rate)
        merged: dict[str, str] = {"User-Agent": user_agent, "Accept": "*/*"}
        merged.update(
            {name: resolve_env_value(str(value)) for name, value in dict(headers or {}).items()}
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            follow_redirects=False,
            headers=merged,
            cookies=dict(cookies or {}),
            verify=verify,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            transport=transport,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        method: Method = "GET",
        want_body: bool = False,
        max_hops: int | None = None,
    ) -> FetchResult:
        hops = self.max_hops if max_hops is None else int(max_hops)
        timing = _Timing()
        result: FetchResult | None = None
        for attempt in range(self.retries + 1):
            result = await self._fetch_once(
                url, method=method, want_body=want_body, max_hops=hops, timing=timing
            )
            result.duration_ms = max(0, int(timing.network * 1000))
            result.waited_ms = max(0, int(timing.waited * 1000))
            result.attempts = attempt + 1
            if not self._should_retry(result):
                return result
            if attempt < self.retries:
                await asyncio.sleep(min(2.0, 0.25 * (attempt + 1)))
        assert result is not None
        return result

    def _should_retry(self, result: FetchResult) -> bool:
        if result.error is not None and result.error.kind in TRANSIENT_ERRORS:
            return True
        return result.status is not None and result.status in self.retry_statuses

    async def _fetch_once(
        self,
        url: str,
        *,
        method: Method,
        want_body: bool,
        max_hops: int,
        timing: _Timing | None = None,
    ) -> FetchResult:
        stats = timing if timing is not None else _Timing()
        chain: list[RedirectHop] = []
        seen: set[str] = set()
        current = url
        status: int | None = None
        headers: Mapping[str, str] = {}
        body: bytes | None = None
        size: int | None = None
        truncated = False
        content_type: str | None = None

        for _ in range(max_hops + 1):
            stats.waited += await self._limiter.acquire(_host_of(current))
            began = self._clock()
            try:
                status, headers, body, size, truncated = await self._request_once(
                    current, method=method, want_body=want_body
                )
            except (httpx.HTTPError, OSError, ValueError) as exc:
                stats.network += self._clock() - began
                return FetchResult(
                    url=url,
                    method=method,
                    status=None,
                    final_url=current,
                    headers=headers,
                    chain=chain,
                    error=classify_exception(exc),
                )
            stats.network += self._clock() - began
            location = headers.get("location")
            chain.append(RedirectHop(url=current, status=status, location=location))
            content_type = headers.get("content-type")
            if status in REDIRECT_STATUSES and location:
                nxt = urljoin(current, location)
                if nxt in seen or nxt == current:
                    return FetchResult(
                        url=url,
                        method=method,
                        status=status,
                        final_url=nxt,
                        headers=headers,
                        chain=chain,
                        content_type=content_type,
                        error=FetchError(ERROR_LOOP, f"redirect loop back to {nxt}"),
                    )
                seen.add(current)
                current = nxt
                body = None
                size = None
                continue
            if status in REDIRECT_STATUSES:
                return FetchResult(
                    url=url,
                    method=method,
                    status=status,
                    final_url=current,
                    headers=headers,
                    chain=chain,
                    error=FetchError(
                        ERROR_BAD_LOCATION, f"HTTP {status} redirect with no Location header"
                    ),
                )
            if truncated:
                return FetchResult(
                    url=url,
                    method=method,
                    status=status,
                    final_url=current,
                    headers=headers,
                    chain=chain,
                    content_type=content_type,
                    size_bytes=size,
                    truncated=True,
                    error=FetchError(
                        ERROR_TOO_LARGE,
                        f"response body exceeds the {self._max_bytes_text()} parse limit",
                    ),
                )
            return FetchResult(
                url=url,
                method=method,
                status=status,
                final_url=current,
                headers=headers,
                chain=chain,
                body=body,
                content_type=content_type,
                size_bytes=size,
                truncated=truncated,
            )

        return FetchResult(
            url=url,
            method=method,
            status=status,
            final_url=current,
            headers=headers,
            chain=chain,
            content_type=content_type,
            error=FetchError(
                ERROR_TOO_MANY_HOPS,
                f"more than {max_hops} redirects ({len(chain)} hops recorded)",
            ),
        )

    def _max_bytes_text(self) -> str:
        return f"{self.max_bytes / 1_000_000:.1f} MB"

    async def _request_once(
        self, url: str, *, method: Method, want_body: bool
    ) -> tuple[int | None, Mapping[str, str], bytes | None, int | None, bool]:
        request = self._client.build_request(method, url)
        response = await self._client.send(request, stream=True)
        try:
            read_body = (
                want_body and method != "HEAD" and response.status_code not in REDIRECT_STATUSES
            )
            if not read_body:
                return response.status_code, dict(response.headers), None, None, False
            chunks: list[bytes] = []
            total = 0
            too_big = False
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > self.max_bytes:
                    too_big = True
                    break
            return (
                response.status_code,
                dict(response.headers),
                None if too_big else b"".join(chunks),
                total,
                too_big,
            )
        finally:
            await response.aclose()


def resolve_env_value(value: str) -> str:
    """``"env:LINKKEEP_TOKEN"`` -> the variable's value (empty string when unset)."""
    if value.startswith("env:"):
        return os.environ.get(value[4:].strip(), "")
    return value


def redact_mapping(values: Mapping[str, object] | None) -> dict[str, str]:
    """Replace every value with ``***``; used before config is written to the database."""
    return {str(name): "***" for name in (values or {})}


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Mask only sensitive headers (for ``--verbose`` logging)."""
    return {
        name: ("***" if name.lower() in SENSITIVE_HEADERS else str(value))
        for name, value in headers.items()
    }


def _host_of(url: str) -> str:
    try:
        return (httpx.URL(url).host or "").lower()
    except httpx.InvalidURL:
        return url
