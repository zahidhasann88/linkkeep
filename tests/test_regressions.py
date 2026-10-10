"""Regression checks for the pre-publishing review."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from tests.conftest import BASE, NOW, FakeFetcher, page
from tests.report_samples import sample_report_data

from linkkeep.config import Config, ConfigError, read_yaml_config
from linkkeep.crawler import Crawler
from linkkeep.fetcher import HttpxFetcher
from linkkeep.models import Counts, RunStatus
from linkkeep.ratelimit import RateLimiter
from linkkeep.report import render
from linkkeep.robots import RobotsClient
from linkkeep.storage import Storage
from linkkeep.urlnorm import normalise_url, parse_url


async def public_dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def test_private_redirect_never_reaches_transport() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async with HttpxFetcher(
        transport=httpx.MockTransport(handler),
        allow_private=False,
        resolver=public_dns,
        retries=0,
        rate=1e9,
    ) as fetcher:
        result = await fetcher.fetch("https://public.test/")
    assert len(calls) == 1
    assert result.error is not None
    assert "private" in result.error.detail


async def test_private_dns_never_reaches_transport() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "169.254.169.254"]

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("private DNS target was requested")

    async with HttpxFetcher(
        transport=httpx.MockTransport(handler),
        allow_private=False,
        resolver=resolver,
        retries=0,
    ) as fetcher:
        result = await fetcher.fetch("https://public.test/")
    assert result.error is not None
    assert "private DNS" in result.error.detail


async def test_auth_is_scoped_to_seed_origin_including_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://third.test/"})
        return httpx.Response(200)

    async with HttpxFetcher(
        transport=httpx.MockTransport(handler),
        rate=1e9,
        retries=0,
        auth_origins=[BASE],
        headers={"Authorization": "secret", "X-Custom-Key": "hidden"},
        cookies={"session": "cookie-secret"},
    ) as fetcher:
        await fetcher.fetch(BASE + "/")
        await fetcher.fetch("http://site.test/downgrade")
    assert seen[0].headers["authorization"] == "secret"
    for request in seen[1:]:
        assert not {"authorization", "cookie", "x-custom-key"} & set(request.headers)


async def test_concurrent_robots_wait_for_rules() -> None:
    class SlowFetcher(FakeFetcher):
        async def fetch(self, url: str, **kwargs: object):
            await asyncio.sleep(0.01)
            return await super().fetch(url, **kwargs)

    fetcher = SlowFetcher({BASE + "/robots.txt": {"body": "User-agent: *\nDisallow: /private"}})
    robots = RobotsClient(fetcher, "Linkkeep/test")
    decisions = await asyncio.gather(*(robots.decision(BASE + "/private") for _ in range(5)))
    assert all(not item.allowed for item in decisions)
    assert fetcher.count_for(BASE + "/robots.txt") == 1


async def test_fragments_found_after_incremental_flush(tmp_path: Path) -> None:
    routes = {
        BASE + "/": page(
            "".join(f'<a href="/p{i}">p</a>' for i in range(30))
            + '<a href="/last#missing">last</a>'
        ),
        BASE + "/last": page("<p>no anchor</p>"),
    }
    routes.update({BASE + f"/p{i}": page("<p>ok</p>") for i in range(30)})
    store = Storage(tmp_path / "fragments.sqlite")
    try:
        crawler = Crawler(
            Config.from_dict({"crawl": {"concurrency": 1}, "rules": {"check_fragments": True}}),
            FakeFetcher(routes),
            storage=store,
        )
        report = await crawler.run([BASE + "/"])
        assert crawler._flushed >= 25
        assert any(item.code == "missing_fragment" for item in report.issues)
    finally:
        store.close()


async def test_external_robots_blocks_status_check() -> None:
    fetcher = FakeFetcher(
        {
            BASE + "/": page('<a href="https://other.test/private">x</a>'),
            "https://other.test/robots.txt": {"body": "User-agent: *\nDisallow: /private"},
        }
    )
    report = await Crawler(Config(), fetcher).run([BASE + "/"])
    assert fetcher.count_for("https://other.test/private") == 0
    assert any(item.code == "blocked_by_robots" for item in report.issues)


def test_fragment_counter_survives_storage(tmp_path: Path) -> None:
    store = Storage(tmp_path / "history.sqlite")
    try:
        run_id = store.create_run(BASE, [BASE], {}, NOW)
        store.save_run_data(run_id, [], [], [], Counts(fragment_only=4), RunStatus.COMPLETED, NOW)
        assert store.get_run(run_id).counts.fragment_only == 4
    finally:
        store.close()


@pytest.mark.parametrize(
    "url", ["https://[bad/", "https://site.test:bad/", "https://site.test:99999/"]
)
def test_invalid_urls_do_not_raise(url: str) -> None:
    assert parse_url(url) is None
    assert normalise_url(url) is None


def test_ipv6_normalization_keeps_brackets() -> None:
    assert normalise_url("http://[::1]:8080/a") == "http://[::1]:8080/a"
    assert parse_url("http://[::1]:8080/a").origin == "http://[::1]:8080"


def test_yaml_error_does_not_reveal_secret(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("auth: [secret-token\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        read_yaml_config(path)
    assert "secret-token" not in str(caught.value)


def test_webhook_is_redacted_in_stored_config() -> None:
    config = Config.from_dict({"watch": {"notify_webhook": "https://hook.test/secret"}})
    assert "secret" not in json.dumps(config.to_stored_dict())


def test_markdown_does_not_embed_hostile_page_html() -> None:
    data = sample_report_data()
    data.issues = [item.model_copy(deep=True) for item in data.issues]
    data.issues[0].link_text = "<img src=x onerror=alert(1)>|`"
    data.issues[0].target_url = "https://site.test/x)![tracking](https://other.test/x)"
    text = render(data, "markdown")
    assert "<img" not in text
    assert ")![tracking]" not in text


def test_junit_drops_noncharacters() -> None:
    import xml.etree.ElementTree as ET

    data = sample_report_data()
    data.issues = [item.model_copy(deep=True) for item in data.issues]
    data.issues[0].link_text = "bad\ufffe\uffff"
    ET.fromstring(render(data, "junit"))


async def test_reserved_invalid_domain_uses_no_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("reserved invalid domain was requested")

    async with HttpxFetcher(transport=httpx.MockTransport(handler), retries=0) as fetcher:
        result = await fetcher.fetch("https://partner.invalid/docs")
    assert result.error is not None
    assert result.error.kind == "dns"


async def test_host_crawl_delay_controls_shared_rate() -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    limiter = RateLimiter(10, clock=clock, sleep=clock.sleep)
    limiter.set_delay("site.test", 3)
    await limiter.acquire("site.test")
    assert await limiter.acquire("site.test") == pytest.approx(3)


async def test_same_page_fragment_is_checked_without_refetch() -> None:
    fetcher = FakeFetcher({BASE + "/": page('<a href="#missing">missing</a><p id="valid">x</p>')})
    report = await Crawler(Config.from_dict({"rules": {"check_fragments": True}}), fetcher).run(
        [BASE + "/"]
    )
    assert [item.target_url for item in report.issues] == [BASE + "/#missing"]
    assert fetcher.count_for(BASE + "/") == 1


async def test_fetch_error_redacts_configured_header_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("header secret-token failed")

    async with HttpxFetcher(
        transport=httpx.MockTransport(handler), headers={"Authorization": "secret-token"}, retries=0
    ) as fetcher:
        result = await fetcher.fetch(BASE)
    assert result.error is not None
    assert "secret-token" not in result.error.detail


def test_default_diff_selects_latest_site(tmp_path: Path) -> None:
    from linkkeep.diff import resolve_run_ids

    store = Storage(tmp_path / "mixed.sqlite")
    try:
        first = store.create_run(BASE, [BASE], {}, NOW)
        store.create_run("https://other.test", [], {}, NOW)
        newest = store.create_run(BASE, [BASE], {}, NOW)
        assert resolve_run_ids(store, None, None) == (first, newest)
    finally:
        store.close()


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_nonfinite_request_limits_are_rejected(value: float) -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"crawl": {"rate": value}})
