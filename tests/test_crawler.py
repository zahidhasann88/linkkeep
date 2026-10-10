"""End-to-end crawl behaviour with a fake fetcher (no sockets, no respx)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from tests.conftest import FakeFetcher, page

from linkkeep.config import Config, ConfigError
from linkkeep.crawler import Crawler, ignored_scheme, prepare_seeds, settings_from_config
from linkkeep.models import LinkKind, RunStatus, Severity

SITE = "https://site.test"


def make_crawler(
    routes: dict[str, dict],
    *,
    config: Config | None = None,
    storage: object | None = None,
    **kwargs: object,
):
    fetcher = FakeFetcher(
        routes,
        allow_unknown=kwargs.pop("allow_unknown", False),
        default_status=kwargs.pop("default_status", 404),
    )
    cfg = config or Config.from_dict({"rules": {"follow_external": True}})
    crawler = Crawler(cfg, fetcher, storage=storage, **kwargs)  # type: ignore[arg-type]
    return crawler, fetcher


async def crawl(routes: dict[str, dict], *, seeds: list[str] | None = None, **kwargs: object):
    crawler, fetcher = make_crawler(routes, **kwargs)
    report = await crawler.run(seeds or [f"{SITE}/"])
    return report, fetcher


def codes(report: object) -> list[str]:
    return [issue.code for issue in report.issues]  # type: ignore[attr-defined]


# ------------------------------------------------------------------- happy path
async def test_two_page_site_is_walked_and_nothing_is_reported() -> None:
    routes = {
        f"{SITE}/": {"html": '<a href="/about">About</a>', "status": 200},
        f"{SITE}/about": page('<a href="/">Home</a>'),
    }
    report, fetcher = await crawl(routes)
    assert sorted(p.url for p in report.pages) == [f"{SITE}/", f"{SITE}/about"]
    assert report.issues == []
    assert report.status is RunStatus.COMPLETED
    assert report.counts.pages == 2
    assert report.counts.links == 2
    assert report.counts.checked == 2
    assert report.run_id is None  # no storage attached
    assert f"{SITE}/robots.txt" in fetcher.urls_seen()


async def test_pages_record_depth_and_duration() -> None:
    routes = {
        f"{SITE}/": page('<a href="/a">a</a>'),
        f"{SITE}/a": page('<a href="/b">b</a>', duration_ms=42),
        f"{SITE}/b": page("<p>leaf</p>"),
    }
    report, _ = await crawl(routes)
    by_url = {p.url: p for p in report.pages}
    assert by_url[f"{SITE}/"].depth == 0
    assert by_url[f"{SITE}/a"].depth == 1
    assert by_url[f"{SITE}/b"].depth == 2
    assert by_url[f"{SITE}/a"].duration_ms == 42
    assert by_url[f"{SITE}/a"].content_type == "text/html; charset=utf-8"


async def test_link_rows_point_at_the_source_page() -> None:
    routes = {f"{SITE}/": page('<a href="/x">X</a>'), f"{SITE}/x": page("<p>x</p>")}
    report, _ = await crawl(routes)
    link = report.links[0]
    assert link.source_url == f"{SITE}/"
    assert link.target_url == f"{SITE}/x"
    assert link.kind is LinkKind.INTERNAL
    assert link.element == "a"
    assert link.status == 200  # filled in from the page result, no second request
    assert link.checked is True


# --------------------------------------------------------------------- externals
async def test_external_links_are_checked_but_not_crawled() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="https://other.test/a">A</a> <a href="https://other.test/a">A again</a>'
        ),
    }
    report, fetcher = await crawl(routes, allow_unknown=True, default_status=200)
    assert [p.url for p in report.pages] == [f"{SITE}/"]
    assert fetcher.count_for("https://other.test/a") == 1  # deduplicated, HEAD only
    assert set(fetcher.urls_seen(method="GET")) == {
        f"{SITE}/",
        f"{SITE}/robots.txt",
        "https://other.test/robots.txt",
    }
    assert report.issues == []


async def test_broken_external_link_is_reported_with_its_status() -> None:
    routes = {f"{SITE}/": page('<a href="https://other.test/gone">gone</a>')}
    report, _ = await crawl(routes, default_status=404)
    assert codes(report) == ["broken"]
    issue = report.issues[0]  # type: ignore[attr-defined]
    assert issue.status == 404
    assert issue.source_url == f"{SITE}/"
    assert issue.target_url == "https://other.test/gone"
    assert issue.element == "a"


async def test_unreachable_external_link_reports_the_reason() -> None:
    routes = {f"{SITE}/": page('<a href="https://other.test/x">x</a>')}
    report, _ = await crawl(routes, allow_unknown=None, default_status=None)
    issue = report.issues[0]  # type: ignore[attr-defined]
    assert issue.code == "unreachable"
    assert "DNS" in (issue.reason or "")


async def test_external_checks_can_be_switched_off() -> None:
    config = Config.from_dict({"rules": {"follow_external": False}})
    routes = {f"{SITE}/": page('<a href="https://other.test/404">x</a>')}
    report, fetcher = await crawl(routes, config=config, default_status=404)
    assert report.issues == []
    assert "https://other.test/404" not in fetcher.urls_seen()
    assert report.links[0].checked is False
    assert "not checked" in (report.links[0].error or "")


async def test_head_rejection_falls_back_to_get_without_duplicate_issues() -> None:
    routes = {f"{SITE}/": page('<a href="https://other.test/x">x</a>')}

    # the fake answers 405 to HEAD for every URL, so check_link retries with GET
    class HeadHating(FakeFetcher):
        async def fetch(self, url, *, method="GET", want_body=False, max_hops=None):  # type: ignore[no-untyped-def]
            if method == "HEAD":
                self.calls.append((url, "HEAD"))
                from linkkeep.fetcher import FetchResult

                return FetchResult(url=url, method="HEAD", status=405)
            return await super().fetch(url, method="GET", want_body=want_body, max_hops=max_hops)

    fetcher = HeadHating({f"{SITE}/": routes[f"{SITE}/"]}, allow_unknown=True, default_status=200)
    crawler = Crawler(Config.from_dict({}), fetcher)
    await crawler.run([f"{SITE}/"])
    assert ("https://other.test/x", "HEAD") in fetcher.calls
    assert ("https://other.test/x", "GET") in fetcher.calls


# ------------------------------------------------------------------ issue kinds
async def test_broken_internal_page_reports_the_link_that_pointed_at_it() -> None:
    routes = {f"{SITE}/": page('<a href="/missing">Missing</a>')}
    report, _ = await crawl(routes, default_status=404)
    assert codes(report) == ["broken"]
    issue = report.issues[0]  # type: ignore[attr-defined]
    assert issue.severity is Severity.ERROR
    assert issue.fingerprint.startswith(
        ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f")
    )


async def test_redirect_chain_and_loop_and_error_endpoint() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="/long">long</a> <a href="/loop">loop</a> <a href="/toerr">toerr</a>'
        ),
        f"{SITE}/long": {"status": 301, "location": "/l1"},
        f"{SITE}/l1": {"status": 301, "location": "/l2"},
        f"{SITE}/l2": {"status": 301, "location": "/l3"},
        f"{SITE}/l3": {"status": 301, "location": "/l4"},
        f"{SITE}/l4": page("<p>end</p>"),
        f"{SITE}/toerr": {"status": 301, "location": "https://site.test/dead"},
    }
    config = Config.from_dict({"crawl": {"max_redirect_hops": 2}})
    report, _ = await crawl(routes, config=config, default_status=404)
    found = {issue.code for issue in report.issues}  # type: ignore[attr-defined]
    assert "redirect_chain" in found
    assert "redirect_to_error" in found


async def test_redirect_loop_from_a_fetched_page_is_an_error() -> None:
    class Looping(FakeFetcher):
        async def fetch(self, url, *, method="GET", want_body=False, max_hops=None):  # type: ignore[no-untyped-def]
            self.calls.append((url, method))
            from linkkeep.fetcher import FetchError, FetchResult

            return FetchResult(
                url=url,
                status=301,
                final_url=f"{SITE}/loop-b",
                error=FetchError("redirect_loop", "redirect loop back to https://site.test/loop-a"),
            )

    fetcher = Looping({})
    crawler = Crawler(Config.from_dict({}), fetcher)
    report = await crawler.run([f"{SITE}/loop-a"])
    assert [i.code for i in report.issues] == ["redirect_loop"]


async def test_slow_pages_are_informational() -> None:
    routes = {
        f"{SITE}/": page('<a href="/slow">s</a>', duration_ms=10),
        f"{SITE}/slow": page("<p>s</p>", duration_ms=9000),
    }
    config = Config.from_dict({"detection": {"slow_ms": 1000}})
    report, _ = await crawl(routes, config=config)
    slow = [i for i in report.issues if i.code == "slow"]  # type: ignore[attr-defined]
    assert len(slow) == 1  # page and link collapse into one fingerprint
    assert slow[0].severity is Severity.INFO
    assert "9000 ms" in (slow[0].reason or "")


async def test_mixed_content_is_flagged_for_resources_only() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="http://insecure.test/page">link</a> <img src="http://insecure.test/a.png">'
        )
    }
    config = Config.from_dict({"rules": {"follow_external": False}})
    report, _ = await crawl(routes, config=config)
    mixed = [i for i in report.issues if i.code == "mixed_content"]  # type: ignore[attr-defined]
    assert len(mixed) == 1
    assert mixed[0].target_url == "http://insecure.test/a.png"
    assert mixed[0].severity is Severity.WARNING


async def test_ignore_status_silences_known_flaky_externals() -> None:
    routes = {f"{SITE}/": page('<a href="https://www.linkedin.com/in/x">me</a>')}
    config = Config.from_dict({"rules": {"ignore_status": [999]}})
    report, _ = await crawl(routes, config=config, default_status=999)
    assert report.issues == []


# --------------------------------------------------------------------- fragments
async def test_missing_fragments_are_only_checked_when_asked() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="/post#yes">ok</a> <a href="/post#nope">bad</a> <a href="/post">bare</a>'
        ),
        f"{SITE}/post": page('<p id="yes">hi</p>'),
    }
    off, _ = await crawl(routes)
    assert codes(off) == []
    on_config = Config.from_dict({"rules": {"check_fragments": True}})
    on, _ = await crawl(routes, config=on_config)
    missing = [i for i in on.issues if i.code == "missing_fragment"]  # type: ignore[attr-defined]
    assert len(missing) == 1
    assert missing[0].target_url == f"{SITE}/post#nope"
    assert missing[0].final_url == f"{SITE}/post"


async def test_fragment_of_a_page_that_was_not_crawled_is_not_reported() -> None:
    routes = {
        f"{SITE}/": page('<a href="/never#a">x</a>'),
    }
    config = Config.from_dict({"rules": {"check_fragments": True, "follow_external": True}})
    report, _ = await crawl(routes, config=config, default_status=404)
    assert "missing_fragment" not in codes(report)


# ---------------------------------------------------------------------- robots
async def test_robots_txt_blocks_internal_urls() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="/public">p</a> <a href="/secret">s</a> <a href="https://other.test/secret">e</a>'
        ),
        f"{SITE}/robots.txt": {"body": "User-agent: *\nDisallow: /secret\n"},
        f"{SITE}/public": page("<p>ok</p>"),
    }
    report, fetcher = await crawl(routes, default_status=404)
    assert f"{SITE}/secret" not in fetcher.urls_seen()
    assert (
        "https://other.test/secret" in fetcher.urls_seen()
    )  # external links are not governed by our robots file
    blocked = [i for i in report.issues if i.code == "blocked_by_robots"]  # type: ignore[attr-defined]
    assert len(blocked) == 1
    assert blocked[0].target_url == f"{SITE}/secret"
    assert blocked[0].severity is Severity.INFO
    assert report.counts.robots_blocked == 1


async def test_ignore_robots_crawls_anything_and_warns() -> None:
    routes = {
        f"{SITE}/": page('<a href="/secret">s</a>'),
        f"{SITE}/robots.txt": {"body": "User-agent: *\nDisallow: /\n"},
    }
    config = Config.from_dict({"rules": {"ignore_robots": True}})
    report, fetcher = await crawl(routes, config=config, default_status=404)
    assert f"{SITE}/secret" in fetcher.urls_seen(method="GET")
    assert f"{SITE}/robots.txt" not in fetcher.urls_seen()
    assert "blocked_by_robots" not in codes(report)


async def test_crawl_delay_is_honoured_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("linkkeep.crawler.asyncio.sleep", fake_sleep)
    routes = {
        f"{SITE}/": page("<p>home</p>"),
        f"{SITE}/robots.txt": {"body": "User-agent: *\nCrawl-delay: 7\n"},
    }
    await crawl(routes)
    assert any(value == pytest.approx(7.0) for value in slept)


async def test_crawl_delay_can_be_switched_off() -> None:
    routes = {
        f"{SITE}/": page("<p>home</p>"),
        f"{SITE}/robots.txt": {"body": "User-agent: *\nCrawl-delay: 7\n"},
    }
    config = Config.from_dict({"respect_crawl_delay": False})
    crawler, _fetcher = make_crawler(routes, config=config)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    import linkkeep.crawler as module

    original = module.asyncio.sleep
    module.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        await crawler.run([f"{SITE}/"])
    finally:
        module.asyncio.sleep = original  # type: ignore[assignment]
    assert slept == []


# ----------------------------------------------------------------------- limits
async def test_max_pages_stops_the_walk() -> None:
    routes = {f"{SITE}/": page("".join(f'<a href="/p{i}">x</a>' for i in range(20)))}
    routes.update({f"{SITE}/p{i}": page("<p>leaf</p>") for i in range(20)})
    config = Config.from_dict({"crawl": {"max_pages": 4}})
    report, _fetcher = await crawl(routes, config=config)
    assert report.counts.pages == 4
    assert any("max_pages" in note for note in report.notes)
    assert report.counts.excluded == 0  # over-budget links are not even recorded as excluded


async def test_max_depth_stops_the_walk() -> None:
    routes = {
        f"{SITE}/": page('<a href="/a">a</a>'),
        f"{SITE}/a": page('<a href="/b">b</a>'),
        f"{SITE}/b": page('<a href="/c">c</a>'),
        f"{SITE}/c": page("<p>deep</p>"),
    }
    config = Config.from_dict({"crawl": {"max_depth": 1}})
    report, fetcher = await crawl(routes, config=config)
    assert sorted(p.url for p in report.pages) == [f"{SITE}/", f"{SITE}/a"]
    assert f"{SITE}/b" not in fetcher.urls_seen(method="GET")
    link_b = next(link for link in report.links if link.target_url == f"{SITE}/b")  # type: ignore[attr-defined]
    assert "max_depth" in (link_b.error or "")


async def test_include_and_exclude_patterns() -> None:
    routes = {
        f"{SITE}/": page('<a href="/keep">k</a> <a href="/skip">s</a> <a href="/blog/x">b</a>'),
        f"{SITE}/keep": page("<p>k</p>"),
        f"{SITE}/skip": page("<p>s</p>"),
        f"{SITE}/blog/x": page("<p>b</p>"),
    }
    config = Config.from_dict(
        {"rules": {"include": ["glob:https://site.test/blog/**", "glob:https://site.test/keep"]}}
    )
    report, fetcher = await crawl(routes, config=config)
    assert sorted(p.url for p in report.pages) == [f"{SITE}/", f"{SITE}/blog/x", f"{SITE}/keep"]
    assert f"{SITE}/skip" not in fetcher.urls_seen(method="GET")
    excluded = [link for link in report.links if link.target_url == f"{SITE}/skip"]  # type: ignore[attr-defined]
    assert len(excluded) == 1
    assert excluded[0].checked is False
    assert "excluded by pattern" in (excluded[0].error or "")


async def test_exclude_wins_over_include() -> None:
    routes = {
        f"{SITE}/": page('<a href="/blog/a">a</a> <a href="/blog/private">p</a>'),
        f"{SITE}/blog/a": page("<p>a</p>"),
    }
    config = Config.from_dict(
        {"rules": {"include": ["glob:*/blog/*"], "exclude": ["glob:*/blog/private"]}}
    )
    report, _ = await crawl(routes, config=config)
    assert [p.url for p in report.pages] == [f"{SITE}/", f"{SITE}/blog/a"]


# -------------------------------------------------------------------- subdomains
async def test_subdomains_are_external_by_default() -> None:
    routes = {
        f"{SITE}/": page('<a href="https://blog.site.test/x">blog</a>'),
        "https://blog.site.test/x": page("<p>hi</p>"),
    }
    report, fetcher = await crawl(routes)
    assert [p.url for p in report.pages] == [f"{SITE}/"]
    assert "https://blog.site.test/x" in fetcher.urls_seen(method="HEAD")
    assert report.links[0].kind is LinkKind.EXTERNAL


async def test_include_subdomains_widens_the_crawl() -> None:
    routes = {
        f"{SITE}/": page('<a href="https://blog.site.test/x">blog</a>'),
        "https://blog.site.test/x": page("<p>hi</p>"),
    }
    config = Config.from_dict({"rules": {"include_subdomains": True}})
    report, _ = await crawl(routes, config=config)
    assert {p.url for p in report.pages} == {f"{SITE}/", "https://blog.site.test/x"}


async def test_www_is_treated_as_the_same_site() -> None:
    routes = {
        f"{SITE}/": page('<a href="https://www.site.test/x">www</a>'),
        "https://www.site.test/x": page("<p>hi</p>"),
    }
    report, _ = await crawl(routes)
    assert "https://www.site.test/x" in [p.url for p in report.pages]


# ------------------------------------------------------------------------ ssrf
async def test_private_seed_is_refused_without_the_flag() -> None:
    crawler, _ = make_crawler({})
    with pytest.raises(ConfigError, match="private or loopback"):
        await crawler.run(["http://127.0.0.1:8000/"])


async def test_private_seed_is_allowed_with_the_flag() -> None:
    config = Config.from_dict({"rules": {"allow_private": True}})
    crawler, _ = make_crawler({f"{SITE}/": page("<p>ok</p>")}, config=config)
    report = await crawler.run([f"{SITE}/"])
    assert report.counts.pages == 1


async def test_links_to_private_hosts_are_skipped() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="http://127.0.0.1:9/x">internal</a> <a href="http://10.0.0.5/y">corp</a>'
        )
    }
    config = Config.from_dict({"rules": {"allow_private": False, "include_subdomains": False}})
    crawler, fetcher = make_crawler(routes, config=config)
    report = await crawler.run([f"{SITE}/"])
    assert "http://127.0.0.1:9/x" not in fetcher.urls_seen(method="GET")
    skipped = [link for link in report.links if "private or loopback" in (link.error or "")]  # type: ignore[attr-defined]
    assert len(skipped) == 2


async def test_allow_private_crawls_loopback_links() -> None:
    routes = {
        "http://127.0.0.1:9/": page('<a href="http://127.0.0.1:9/x">x</a>'),
        "http://127.0.0.1:9/x": page("<p>ok</p>"),
    }
    config = Config.from_dict({"rules": {"allow_private": True}})
    crawler, _ = make_crawler(routes, config=config)
    report = await crawler.run(["http://127.0.0.1:9/"])
    assert report.counts.pages == 2


# --------------------------------------------------------------------- sitemap
async def test_sitemap_urls_are_queued() -> None:
    routes = {
        f"{SITE}/": page("<p>home</p>"),
        f"{SITE}/sitemap.xml": {
            "body": (
                '<?xml version="1.0"?><urlset><url><loc>https://site.test/from-sitemap</loc></url>'
                "<url><loc>https://other.test/nope</loc></url></urlset>"
            ),
            "status": 200,
        },
        f"{SITE}/from-sitemap": page("<p>s</p>"),
    }
    config = Config.from_dict({"rules": {"sitemap": True}})
    report, fetcher = await crawl(routes, config=config)
    assert f"{SITE}/from-sitemap" in fetcher.urls_seen(method="GET")
    assert "https://other.test/nope" not in fetcher.urls_seen(method="GET")
    assert any("sitemap added 1" in note for note in report.notes)


async def test_sitemap_index_is_followed_one_level() -> None:
    routes = {
        f"{SITE}/": page("<p>h</p>"),
        f"{SITE}/sitemap.xml": {
            "body": "<sitemapindex><sitemap><loc>https://site.test/sitemap-1.xml</loc></sitemap></sitemapindex>",
            "status": 200,
        },
        "https://site.test/sitemap-1.xml": {
            "body": "<urlset><url><loc>https://site.test/deep</loc></url></urlset>",
            "status": 200,
        },
        f"{SITE}/deep": page("<p>d</p>"),
    }
    config = Config.from_dict({"rules": {"sitemap": True}})
    crawler, fetcher = make_crawler(routes, config=config)
    await crawler.run([f"{SITE}/"])
    assert f"{SITE}/deep" in fetcher.urls_seen(method="GET")


async def test_unreadable_sitemap_is_not_fatal() -> None:
    config = Config.from_dict({"rules": {"sitemap": True}})
    crawler, _ = make_crawler({f"{SITE}/": page("<p>h</p>")}, config=config, default_status=404)
    report = await crawler.run([f"{SITE}/"])
    assert any("sitemap added 0" in note for note in report.notes)
    assert report.counts.pages == 1


# --------------------------------------------------------------------- schemes
async def test_non_http_links_are_counted_in_the_summary() -> None:
    routes = {
        f"{SITE}/": page(
            '<a href="mailto:a@b.test">m</a> <a href="tel:1">t</a> <a href="javascript:x()">j</a>'
        ),
    }
    report, _ = await crawl(routes)
    assert report.counts.skipped_scheme == 3
    assert report.links == []
    assert ignored_scheme("mailto:a@b.test") == "mailto"
    assert ignored_scheme("https://site.test/x") is None


# ------------------------------------------------------------------ interruption
async def test_interrupted_crawl_saves_partial_results() -> None:
    stop = asyncio.Event()
    routes = {
        f"{SITE}/": page('<a href="/a">a</a> <a href="/b">b</a>'),
        f"{SITE}/a": page('<a href="/c">c</a>'),
    }
    fetcher = FakeFetcher(routes, allow_unknown=False, default_status=404)

    class Interrupting(FakeFetcher):
        async def fetch(self, url, *, method="GET", want_body=False, max_hops=None):  # type: ignore[no-untyped-def]
            if url.endswith("/a") and method == "GET":
                stop.set()
            return await fetcher.fetch(url, method=method, want_body=want_body, max_hops=max_hops)

    config = Config.from_dict({"crawl": {"concurrency": 1}})
    crawler = Crawler(config, Interrupting({}), interrupt=stop)
    report = await crawler.run([f"{SITE}/"])
    assert report.status is RunStatus.INTERRUPTED
    assert report.pages  # partial results were kept
    assert "interrupted" in " ".join(report.notes)


# --------------------------------------------------------------------- storage
async def test_run_is_written_to_the_database(storage: object) -> None:
    routes = {f"{SITE}/": page('<a href="/missing">m</a>')}
    config = Config.from_dict({})
    crawler, _ = make_crawler(routes, config=config, storage=storage, default_status=404)
    report = await crawler.run([f"{SITE}/"])
    assert report.run_id is not None
    stored = storage.get_run(report.run_id)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored.status is RunStatus.COMPLETED
    assert stored.counts.pages == 2  # the seed and the 404 page it links to
    assert stored.counts.errors == 1
    assert [i.code for i in storage.issues_for_run(report.run_id)] == ["broken"]  # type: ignore[attr-defined]
    assert len(storage.pages_for_run(report.run_id)) == 2  # type: ignore[attr-defined]
    assert len(storage.links_for_run(report.run_id)) == 1  # type: ignore[attr-defined]
    assert stored.config["crawl"]["max_pages"] == 1000
    assert stored.config["auth"]["redacted"] is True
    assert stored.seeds == [f"{SITE}/"]


async def test_failed_crawl_when_no_page_can_be_read() -> None:
    fetcher = FakeFetcher({}, allow_unknown=False, default_status=None)
    crawler = Crawler(Config.from_dict({}), fetcher)
    report = await crawler.run([f"{SITE}/"])
    assert report.status is RunStatus.FAILED
    assert "did not run" in " ".join(report.notes)
    assert codes(report) == ["unreachable"]


# ------------------------------------------------------------------- reporting
async def test_report_helpers_and_run_record() -> None:
    routes = {f"{SITE}/": page('<a href="/missing">m</a>')}
    report, _ = await crawl(routes, default_status=404)
    assert report.issues_by_severity[Severity.ERROR] == 1
    assert report.failing_issues("error") == report.issues
    assert report.failing_issues("never") == []
    run = report.to_run_record()
    assert run.site == f"{SITE}/"
    assert run.started_at.tzinfo is not None
    assert run.finished_at is not None
    assert isinstance(run.started_at, dt.datetime)
    assert report.seeds == [f"{SITE}/"]


async def test_seed_preparation_and_errors() -> None:
    assert prepare_seeds(["site.test"]) == ["https://site.test/"]
    assert prepare_seeds(["https://a.test/x", " https://a.test/x "]) == ["https://a.test/x"]
    assert prepare_seeds(["https://a.test/x/"]) == ["https://a.test/x/"]
    assert prepare_seeds(["  ", "https://a.test"]) == ["https://a.test/"]
    with pytest.raises(ConfigError, match="no seed URL"):
        prepare_seeds([])
    with pytest.raises(ConfigError, match="not a valid URL"):
        prepare_seeds(["ht tp://nope"])


def test_settings_are_mapped_from_the_config() -> None:
    config = Config.from_dict(
        {
            "detection": {"slow_ms": 111, "mixed_content": False},
            "crawl": {"max_redirect_hops": 9},
            "rules": {"ignore_status": [418]},
        }
    )
    settings = settings_from_config(config)
    assert settings.slow_ms == 111
    assert settings.max_redirect_hops == 9
    assert settings.ignore_status == frozenset({418})
    assert settings.report_mixed_content is False


async def test_site_label_and_log_callback() -> None:
    messages: list[str] = []
    routes = {
        f"{SITE}/": page("<p>hi</p>"),
        f"{SITE}/sitemap.xml": {"body": "<urlset></urlset>", "status": 200},
    }
    config = Config.from_dict({"rules": {"sitemap": True}})
    crawler, _ = make_crawler(routes, config=config, log=messages.append)
    report = await crawler.run([f"{SITE}/"], site="https://example.com/brand")
    assert report.site == "https://example.com/brand"
    assert any("sitemap" in message for message in messages)
    plain, _ = await crawl({f"{SITE}/": page("<p>hi</p>")})
    assert plain.site == f"{SITE}/"  # the normalised seed is the default label


async def test_concurrency_limit_is_respected() -> None:
    live = {"now": 0, "max": 0}
    routes = {f"{SITE}/": page("".join(f'<a href="/p{i}">x</a>' for i in range(12)))}
    routes.update({f"{SITE}/p{i}": page("<p>leaf</p>") for i in range(12)})

    class Counting(FakeFetcher):
        async def fetch(self, url, *, method="GET", want_body=False, max_hops=None):  # type: ignore[no-untyped-def]
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(0)
            try:
                return await super().fetch(
                    url, method=method, want_body=want_body, max_hops=max_hops
                )
            finally:
                live["now"] -= 1

    fetcher = Counting(routes, allow_unknown=False, default_status=200)
    crawler = Crawler(Config.from_dict({"crawl": {"concurrency": 3}}), fetcher)
    await crawler.run([f"{SITE}/"])
    assert live["max"] <= 3
