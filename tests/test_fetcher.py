"""Fetcher behaviour: manual redirects, HEAD/GET fallback, retries, size limits.

All of this runs against ``httpx.MockTransport`` or ``respx`` - no sockets.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from linkkeep.fetcher import (
    ERROR_BAD_LOCATION,
    ERROR_DNS,
    ERROR_LOOP,
    ERROR_TIMEOUT,
    ERROR_TOO_MANY_HOPS,
    FetchError,
    FetchResult,
    HttpxFetcher,
    classify_exception,
    redact_headers,
    redact_mapping,
    resolve_env_value,
)
from linkkeep.ratelimit import RateLimiter

URL = "https://site.test/page"


class Clock:
    """Monotonic clock that advances by a fixed step every time it is read."""

    def __init__(self, step: float = 0.001) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class WaitingClock:
    """A clock whose ``sleep`` actually moves it forward, like :class:`asyncio` would."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(handler: httpx.AsyncBaseTransport | None = None, **kwargs: object) -> HttpxFetcher:
    defaults: dict[str, object] = {
        "timeout": 2.0,
        "retries": 0,
        "max_hops": 3,
        "user_agent": "Linkkeep/test",
        "clock": Clock(),
        "limiter": RateLimiter(1e9),
    }
    defaults.update(kwargs)
    kwargs2 = dict(defaults)
    if handler is not None:
        kwargs2["transport"] = handler
    return HttpxFetcher(**kwargs2)  # type: ignore[arg-type]


def response(
    *, status: int = 200, headers: dict[str, str] | None = None, content: bytes = b"ok"
) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, content=content)


# --------------------------------------------------------------------- redirects
async def test_no_redirect_records_a_single_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(content=b"<html>hi</html>", headers={"content-type": "text/html"})

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch(URL, want_body=True)
    assert result.status == 200
    assert result.final_url == URL
    assert [hop.status for hop in result.chain] == [200]
    assert result.error is None
    assert b"hi" in (result.body or b"")
    assert result.is_html


async def test_redirect_chain_is_recorded_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return response(status=301, headers={"location": "/b"})
        if request.url.path == "/b":
            return response(status=302, headers={"location": "https://site.test/c"})
        return response(status=200)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert result.status == 200
    assert result.final_url == "https://site.test/c"
    assert [hop.url for hop in result.chain] == [
        "https://site.test/a",
        "https://site.test/b",
        "https://site.test/c",
    ]
    assert [hop.status for hop in result.chain] == [301, 302, 200]
    assert result.redirect_count == 2
    assert result.first_redirect_status == 301
    assert result.error is None


async def test_redirect_loop_is_detected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        target = "/loop-b" if request.url.path == "/loop-a" else "/loop-a"
        return response(status=301, headers={"location": target})

    async with make_fetcher(httpx.MockTransport(handler), max_hops=5) as fetcher:
        result = await fetcher.fetch("https://site.test/loop-a")
    assert result.error is not None
    assert result.error.kind == ERROR_LOOP
    assert "loop" in result.error.detail
    assert len(result.chain) == 2  # a -> b -> (a again)


async def test_self_redirect_is_a_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(status=302, headers={"location": "https://site.test/a"})

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert result.error is not None
    assert result.error.kind == ERROR_LOOP


async def test_too_many_hops_reports_the_chain() -> None:
    steps = iter(["/hop1", "/hop2", "/hop3", "/hop4"])

    def handler(request: httpx.Request) -> httpx.Response:
        return response(status=301, headers={"location": next(steps)})

    async with make_fetcher(httpx.MockTransport(handler), max_hops=2) as fetcher:
        result = await fetcher.fetch("https://site.test/hop0")
    assert result.error is not None
    assert result.error.kind == ERROR_TOO_MANY_HOPS
    assert "more than 2 redirects" in result.error.detail
    assert len(result.chain) == 3


async def test_max_hops_zero_stops_at_first_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(status=301, headers={"location": "/elsewhere"})

    async with make_fetcher(httpx.MockTransport(handler), max_hops=0) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert result.status == 301
    assert result.error is not None
    assert result.error.kind == ERROR_TOO_MANY_HOPS


async def test_redirect_without_location_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(status=301)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert result.error is not None
    assert result.error.kind == ERROR_BAD_LOCATION
    assert "no Location" in result.error.detail


async def test_following_absolute_and_relative_locations() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/a":
            return response(status=301, headers={"location": "//other.test/b"})
        return response(status=200)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert seen == ["https://site.test/a", "https://other.test/b"]
    assert result.final_url == "https://other.test/b"


# ------------------------------------------------------------------- methods
async def test_head_then_get_fallback_when_head_is_rejected() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "HEAD":
            return response(status=405)
        return response(status=200)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.check_link("https://other.test/x")
    assert calls == ["HEAD", "GET"]
    assert result.status == 200
    assert result.head_fell_back is True
    assert result.method == "GET"


async def test_head_result_is_used_when_accepted() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return response(status=200)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.check_link("https://other.test/x")
    assert calls == ["HEAD"]
    assert result.status == 200
    assert result.head_fell_back is False


async def test_check_link_can_skip_head() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return response(status=404)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.check_link("https://other.test/x", prefer_head=False)
    assert calls == ["GET"]
    assert result.status == 404


async def test_falls_back_when_head_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            raise httpx.ConnectError("connection refused", request=request)
        return response(status=200)

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.check_link("https://other.test/x")
    assert result.status == 200
    assert result.head_fell_back is True


# ------------------------------------------------------------------- retries
async def test_transient_server_error_is_retried_then_reported() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return response(status=503)

    async with make_fetcher(httpx.MockTransport(handler), retries=1) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert attempts["n"] == 2
    assert result.attempts == 2
    assert result.status == 503


async def test_429_is_retried_and_can_succeed() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return response(status=429 if attempts["n"] == 1 else 200)

    async with make_fetcher(httpx.MockTransport(handler), retries=1) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert result.status == 200
    assert attempts["n"] == 2


async def test_404_is_never_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return response(status=404)

    async with make_fetcher(httpx.MockTransport(handler), retries=2) as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert attempts["n"] == 1
    assert result.status == 404
    assert result.attempts == 1


async def test_dns_failure_is_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)

    async with make_fetcher(httpx.MockTransport(handler), retries=3) as fetcher:
        result = await fetcher.fetch("https://nope.invalid/x")
    assert result.error is not None
    assert result.error.kind == ERROR_DNS
    assert result.attempts == 1


# --------------------------------------------------------------------- errors
@respx.mock
async def test_timeout_surfaces_as_an_error_not_an_exception() -> None:
    respx.get("https://site.test/slow").mock(side_effect=httpx.ReadTimeout("timed out"))
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch("https://site.test/slow")
    assert result.error is not None
    assert result.error.kind == ERROR_TIMEOUT
    assert result.status is None


@respx.mock
async def test_tls_error_classification() -> None:
    import ssl

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unable to get local certificate", request=request)

    route = respx.get("https://site.test/x").mock(side_effect=boom)
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch("https://site.test/x")
    assert route.called
    assert result.error is not None
    assert result.error.kind in {"tls", "connect"}
    assert classify_exception(ssl.SSLError("bad record mac")).kind == "error"


@respx.mock
async def test_redirect_error_still_returns_the_chain() -> None:
    respx.get("https://site.test/a").mock(
        return_value=httpx.Response(301, headers={"location": "/b"})
    )
    respx.get("https://site.test/b").mock(side_effect=httpx.ConnectError("connection refused"))
    async with make_fetcher() as fetcher:
        result = await fetcher.fetch("https://site.test/a")
    assert result.status is None
    assert result.final_url == "https://site.test/b"
    assert len(result.chain) == 1  # only the successful hop is recorded
    assert result.error is not None
    assert result.error.kind == "connect"


# ------------------------------------------------------------------ size limits
async def test_large_body_is_not_read_and_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>" + b"x" * 5000 + b"</html>"
        )

    async with make_fetcher(httpx.MockTransport(handler), max_bytes=200) as fetcher:
        result = await fetcher.fetch("https://site.test/big", want_body=True)
    assert result.status == 200
    assert result.body is None
    assert result.truncated is True
    assert result.error is not None
    assert result.size_bytes
    assert result.size_bytes > 200


async def test_html_detection_uses_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch("https://site.test/doc.pdf", want_body=True)
    assert result.is_html is False
    assert result.html_body is None


async def test_html_detected_from_sniffed_body_when_type_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body>x</body></html>")

    async with make_fetcher(httpx.MockTransport(handler)) as fetcher:
        result = await fetcher.fetch("https://site.test/x", want_body=True)
    assert result.is_html is True


# ------------------------------------------------------------------ utilities
def test_headers_send_the_user_agent_and_resolve_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKKEEP_TOKEN", "s3cret")
    assert resolve_env_value("env:LINKKEEP_TOKEN") == "s3cret"
    assert resolve_env_value("plain") == "plain"
    assert resolve_env_value("env:NOT_SET_ANYWHERE") == ""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return response(status=200)

    fetcher = make_fetcher(
        httpx.MockTransport(handler),
        headers={"Authorization": "env:LINKKEEP_TOKEN", "X-Extra": "kept"},
    )

    async def go() -> None:
        async with fetcher:
            await fetcher.fetch("https://site.test/a")

    import asyncio

    asyncio.run(go())
    assert seen == ["s3cret"]


def test_redaction_helpers() -> None:
    headers = {"Authorization": "Bearer x", "Cookie": "a=b", "Accept": "*/*"}
    masked = redact_headers(headers)
    assert masked["Authorization"] == "***"
    assert masked["Cookie"] == "***"
    assert masked["Accept"] == "*/*"
    assert redact_mapping({"session": "abc"}) == {"session": "***"}


def test_result_defaults_and_helpers() -> None:
    result = FetchResult(url=URL, status=404)
    assert result.final_url == URL
    assert result.ok is False
    assert result.is_error_status is True
    assert result.error_reason == ""
    assert result.redirect_count == 0
    assert repr(FetchError("dns", "nope"))
    assert str(FetchError("dns", "nope")) == "dns: nope"


async def test_rate_limiting_is_not_reported_as_a_slow_server() -> None:
    """``duration_ms`` is the server's time; our own politeness goes to ``waited_ms``."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/html"}, content="<p>ok</p>")

    network = Clock(step=0.001)  # every request "takes" a millisecond
    waiting = WaitingClock()
    limiter = RateLimiter(0.2, clock=waiting, sleep=waiting.sleep)  # 5 s between requests
    fetcher = HttpxFetcher(
        timeout=2.0,
        retries=0,
        max_hops=3,
        user_agent="Linkkeep/test",
        clock=network,
        limiter=limiter,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        first = await fetcher.fetch(URL)
        second = await fetcher.fetch(URL)
    assert first.duration_ms <= 5
    assert first.waited_ms == 0  # the bucket was full for the first request
    assert second.waited_ms >= 4000  # we chose to wait
    assert second.duration_ms <= 5  # the server was still fast
    assert waiting.sleeps  # and we really did sleep (in fake time)


async def test_duration_covers_every_hop_but_not_retry_sleeps() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "https://site.test/two"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content="<p>ok</p>")

    fetcher = make_fetcher(
        httpx.MockTransport(handler), retries=1, clock=Clock(step=0.001), limiter=RateLimiter(1e9)
    )
    async with fetcher:
        result = await fetcher.fetch(URL)
    assert result.status == 200
    assert len(result.chain) == 2
    assert result.duration_ms <= 10  # two hops of fake 1 ms each
