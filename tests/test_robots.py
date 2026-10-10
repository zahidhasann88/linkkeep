"""robots.txt compliance, caching and the blocked_by_robots reporting."""

from __future__ import annotations

import pytest
from tests.conftest import FakeFetcher

from linkkeep.fetcher import FetchResult
from linkkeep.robots import ROBOTS_ALLOWED_ON_ERROR, RobotsClient, empty_rules

ROBOTS = """User-agent: *
Disallow: /private/
Disallow: /tmp
Allow: /private/public.html
Crawl-delay: 2

User-agent: OtherBot
Disallow: /

Sitemap: https://site.test/sitemap.xml
"""


def robots_fetcher(
    text: str | None = ROBOTS, *, status: int = 200, error: tuple[str, str] | None = None
) -> FakeFetcher:
    if error is not None:
        return FakeFetcher({"https://site.test/robots.txt": {"error": error}}, allow_unknown=False)
    if text is None:
        return FakeFetcher({"https://site.test/robots.txt": {"status": 404}}, allow_unknown=False)
    return FakeFetcher(
        {"https://site.test/robots.txt": {"body": text, "status": status}}, allow_unknown=False
    )


def client(
    fetcher: FakeFetcher, *, enabled: bool = True, agent: str = "Linkkeep/0.1"
) -> RobotsClient:
    return RobotsClient(fetcher, agent, enabled=enabled)


async def test_disallowed_paths_are_blocked() -> None:
    fetcher = robots_fetcher()
    policy = client(fetcher)
    assert not await policy.allows("https://site.test/private/page")
    assert not await policy.allows("https://site.test/tmp")
    assert await policy.allows("https://site.test/public")
    assert await policy.allows("https://site.test/private/public.html")  # explicit Allow wins


async def test_robots_txt_is_fetched_once_per_origin() -> None:
    fetcher = robots_fetcher()
    policy = client(fetcher)
    for url in ("https://site.test/a", "https://site.test/b", "https://site.test/private/c"):
        await policy.allows(url)
    assert fetcher.count_for("https://site.test/robots.txt") == 1


async def test_other_origin_gets_its_own_robots() -> None:
    fetcher = FakeFetcher(
        {
            "https://site.test/robots.txt": {"body": "User-agent: *\nDisallow: /x\n"},
            "https://other.test/robots.txt": {"body": "User-agent: *\nDisallow: /y\n"},
        },
        allow_unknown=False,
    )
    policy = client(fetcher)
    assert not await policy.allows("https://site.test/x")
    assert await policy.allows("https://site.test/y")
    assert not await policy.allows("https://other.test/y")


async def test_missing_robots_txt_allows_everything() -> None:
    fetcher = robots_fetcher(text=None)
    policy = client(fetcher)
    assert await policy.allows("https://site.test/private/page")
    decision = await policy.decision("https://site/test/private")
    assert decision.allowed is True


async def test_server_error_on_robots_txt_allows_everything() -> None:
    for status in (403, 500, 503):
        policy = client(robots_fetcher(status=status))
        assert await policy.allows("https://site.test/private/page")


async def test_network_error_on_robots_txt_allows_everything() -> None:
    policy = client(robots_fetcher(error=("dns", "Name or service not known")))
    assert await policy.allows("https://site.test/private/page")
    decision = await policy.decision("https://site.test/anything")
    assert decision.source == "error"


async def test_disabled_client_never_looks_at_robots() -> None:
    fetcher = robots_fetcher()
    policy = client(fetcher, enabled=False)
    assert await policy.allows("https://site.test/private/page")
    assert fetcher.calls == []
    decision = await policy.decision("https://site.test/private")
    assert decision.source == "disabled"


async def test_crawl_delay_and_sitemaps_are_exposed() -> None:
    policy = client(robots_fetcher())
    assert await policy.crawl_delay("https://site.test/a") == pytest.approx(2.0)
    assert await policy.sitemaps("https://site.test/a") == ["https://site.test/sitemap.xml"]


async def test_crawl_delay_absent_is_none() -> None:
    policy = client(robots_fetcher("User-agent: *\nDisallow:\n"))
    assert await policy.crawl_delay("https://site.test/a") is None
    assert await policy.sitemaps("https://site.test/a") == []


async def test_other_user_agent_rules_do_not_apply_to_us() -> None:
    policy = client(robots_fetcher())
    assert await policy.allows("https://site.test/")  # OtherBot is blocked, we are not
    strict = RobotsClient(robots_fetcher(), "OtherBot")
    assert not await strict.allows("https://site.test/anything")


async def test_robots_file_body_is_read_from_a_get_result() -> None:
    """The client asks for the body explicitly (fake fetchers only return it when asked)."""
    fetcher = robots_fetcher()
    await client(fetcher).allows("https://site.test/x")
    assert fetcher.calls[0][1] == "GET"


async def test_empty_robots_file_is_recorded_as_empty() -> None:
    policy = client(robots_fetcher("   \n  "))
    decision = await policy.decision("https://site.test/whatever")
    assert decision.allowed is True
    assert decision.source == "empty"


def test_allow_on_error_constant_is_documented() -> None:
    assert ROBOTS_ALLOWED_ON_ERROR == "allow"


def test_empty_rules_helper_allows_everything() -> None:
    rules = empty_rules()
    assert rules.can_fetch("https://site.test/anything", "Linkkeep/0.1")


def test_origin_of_falls_back_for_relative_urls() -> None:
    assert RobotsClient.origin_of("relative/path") == "relative/path"
    assert RobotsClient.origin_of("https://site.test:8443/a") == "https://site.test:8443"


async def test_unparsable_robots_does_not_crash() -> None:
    class Weird(FakeFetcher):
        async def fetch(self, url, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append((url, kwargs.get("method", "GET")))
            return FetchResult(url=url, status=200, body=None, content_type="text/plain")

    policy = client(Weird({}))
    assert await policy.allows("https://site.test/private/x")


def test_fetcher_result_without_body_does_not_raise() -> None:
    result = FetchResult(url="https://site.test/robots.txt", status=200, body=None)
    assert result.html_body is None
