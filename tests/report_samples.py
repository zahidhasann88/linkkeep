"""One deterministic :class:`ReportData` (and :class:`DiffResult`) used by the golden tests.

Everything here is fixed: timestamps, fingerprints, ordering.  If a renderer's output
changes, ``make goldens`` regenerates the files under ``tests/goldens`` and the diff in
review shows exactly what changed.
"""

from __future__ import annotations

import datetime as dt

from linkkeep.diff import DiffResult, PersistingIssue, compare_issue_sets
from linkkeep.models import (
    Counts,
    Issue,
    LinkKind,
    LinkRecord,
    PageRecord,
    RedirectHop,
    RunRecord,
    RunStatus,
)
from linkkeep.report import ReportData, build_report_data

SITE = "https://example.com"
STARTED = dt.datetime(2026, 3, 4, 9, 0, 0, tzinfo=dt.timezone.utc)
FINISHED = STARTED + dt.timedelta(seconds=12, microseconds=340000)
OLDER = STARTED - dt.timedelta(days=7)

# fmt: off
ISSUES: list[Issue] = [
    Issue(
        type="broken", fingerprint="a1b2c3d4e5f60708", source_url=f"{SITE}/blog/post-1",
        target_url=f"{SITE}/docs/handbook.pdf", status=404, element="a", attribute="href",
        link_text="Handbook", found_at=STARTED + dt.timedelta(seconds=1),
    ),
    Issue(
        type="broken", fingerprint="a1b2c3d4e5f60709", source_url=f"{SITE}/",
        target_url=f"{SITE}/shop/checkout", status=500, element="a", attribute="href",
        link_text="Checkout", found_at=STARTED + dt.timedelta(seconds=2),
    ),
    Issue(
        type="unreachable", fingerprint="b2c3d4e5f6070809", source_url=f"{SITE}/about",
        target_url="https://cdn.example.test/widgets.js", reason="DNS failure: Name or service not known",
        element="script", attribute="src", found_at=STARTED + dt.timedelta(seconds=3),
    ),
    Issue(
        type="redirect_chain", fingerprint="c3d4e5f60708090a", source_url=f"{SITE}/legacy",
        target_url=f"{SITE}/old/news", final_url=f"{SITE}/news", status=200,
        reason="4 redirects, more than the limit of 3",
        redirect_chain=[
            RedirectHop(url=f"{SITE}/old/news", status=301, location=f"{SITE}/n1"),
            RedirectHop(url=f"{SITE}/n1", status=301, location=f"{SITE}/n2"),
            RedirectHop(url=f"{SITE}/n2", status=301, location=f"{SITE}/n3"),
            RedirectHop(url=f"{SITE}/n3", status=301, location=f"{SITE}/news"),
            RedirectHop(url=f"{SITE}/news", status=200, location=None),
        ],
        element="a", attribute="href", link_text="News", found_at=STARTED + dt.timedelta(seconds=4),
    ),
    Issue(
        type="redirect_to_error", fingerprint="d4e5f60708090a0b", source_url=f"{SITE}/promo",
        target_url=f"{SITE}/click/99", final_url="https://partner.test/expired", status=410,
        reason="redirect chain ends in HTTP 410",
        redirect_chain=[
            RedirectHop(url=f"{SITE}/click/99", status=302, location="https://partner.test/go?x=1"),
            RedirectHop(url="https://partner.test/go?x=1", status=410, location=None),
        ],
        element="a", attribute="href", link_text="Summer sale", found_at=STARTED + dt.timedelta(seconds=5),
    ),
    Issue(
        type="redirect_loop", fingerprint="e5f60708090a0b0c", source_url=f"{SITE}/forum",
        target_url=f"{SITE}/forum/thread/7", reason="redirect loop: redirect loop back to https://example.com/forum/thread/7",
        status=301,
        redirect_chain=[
            RedirectHop(url=f"{SITE}/forum/thread/7", status=301, location=f"{SITE}/login"),
            RedirectHop(url=f"{SITE}/login", status=301, location=f"{SITE}/forum/thread/7"),
        ],
        element="a", attribute="href", found_at=STARTED + dt.timedelta(seconds=6),
    ),
    Issue(
        type="redirect_http_downgrade", fingerprint="f60708090a0b0c0d", source_url=f"{SITE}/secure",
        target_url="https://example.com/login", final_url="http://example.com/login", status=200,
        reason="https URL redirects down to http",
        redirect_chain=[RedirectHop(url="https://example.com/login", status=301, location="http://example.com/login")],
        element="a", attribute="href", link_text="Log in", found_at=STARTED + dt.timedelta(seconds=7),
    ),
    Issue(
        type="redirect_temporary_permanent_internal", fingerprint="0708090a0b0c0d0e",
        source_url=f"{SITE}/", target_url=f"{SITE}/news-2024", final_url=f"{SITE}/news", status=302,
        reason="HTTP 302 is temporary; a permanent internal link should use 301",
        redirect_chain=[RedirectHop(url=f"{SITE}/news-2024", status=302, location=f"{SITE}/news")],
        element="a", attribute="href", found_at=STARTED + dt.timedelta(seconds=8),
    ),
    Issue(
        type="mixed_content", fingerprint="18090a0b0c0d0e0f", source_url=f"{SITE}/pricing",
        target_url="http://cdn.example.com/banner.png", reason="insecure http resource on an https page",
        element="img", attribute="src", link_text="Banner", found_at=STARTED + dt.timedelta(seconds=9),
    ),
    Issue(
        type="slow", fingerprint="290a0b0c0d0e0f10", source_url=f"{SITE}/",
        target_url=f"{SITE}/reports/q1", reason="responded in 8412 ms (limit 5000 ms)", status=200,
        element="a", attribute="href", link_text="Q1 report", found_at=STARTED + dt.timedelta(seconds=10),
    ),
    Issue(
        type="missing_fragment", fingerprint="3a0b0c0d0e0f1011", source_url=f"{SITE}/docs",
        target_url=f"{SITE}/docs/api#authentication", final_url=f"{SITE}/docs/api",
        reason="no id or name 'authentication' on the target page", element="a", attribute="href",
        link_text="Auth", found_at=STARTED + dt.timedelta(seconds=11),
    ),
    Issue(
        type="blocked_by_robots", fingerprint="4b0c0d0e0f101112", source_url=f"{SITE}/internal",
        target_url=f"{SITE}/admin/users", reason="robots.txt disallows Linkkeep for this URL, so it was not crawled or checked",
        element="a", attribute="href", found_at=STARTED + dt.timedelta(seconds=12),
    ),
]

FIXED_LAST_WEEK = Issue(
    type="broken", fingerprint="5c0d0e0f10111213", source_url=f"{SITE}/", target_url=f"{SITE}/tmp/banner.html",
    status=404, element="img", attribute="src", link_text="Banner", found_at=OLDER + dt.timedelta(seconds=1),
)
ALSO_FIXED = Issue(
    type="missing_fragment", fingerprint="6d0e0f1011121314", source_url=f"{SITE}/docs",
    target_url=f"{SITE}/docs/api#legacy", final_url=f"{SITE}/docs/api",
    reason="no id or name 'legacy' on the target page", element="a", attribute="href", found_at=OLDER + dt.timedelta(seconds=2),
)
PERSISTING_SINCE = OLDER
# fmt: on

PAGES = [
    PageRecord(
        run_id=42,
        url=f"{SITE}/",
        depth=0,
        status=200,
        final_url=f"{SITE}/",
        duration_ms=184,
        content_type="text/html; charset=utf-8",
        size_bytes=8123,
        links_found=14,
        links_checked=12,
    ),
    PageRecord(
        run_id=42,
        url=f"{SITE}/blog/post-1",
        depth=1,
        status=200,
        final_url=f"{SITE}/blog/post-1",
        duration_ms=212,
        content_type="text/html; charset=utf-8",
        size_bytes=15230,
        links_found=9,
        links_checked=9,
    ),
    PageRecord(
        run_id=42,
        url=f"{SITE}/docs/api",
        depth=1,
        status=200,
        final_url=f"{SITE}/docs/api",
        duration_ms=190,
        content_type="text/html",
        size_bytes=4021,
        links_found=3,
        links_checked=3,
    ),
    PageRecord(
        run_id=42,
        url=f"{SITE}/docs/handbook.pdf",
        depth=2,
        status=404,
        final_url=f"{SITE}/docs/handbook.pdf",
        duration_ms=97,
        content_type="text/html",
        size_bytes=1200,
        links_found=0,
        links_checked=0,
    ),
    PageRecord(
        run_id=42,
        url=f"{SITE}/pricing",
        depth=1,
        status=200,
        final_url=f"{SITE}/pricing",
        duration_ms=240,
        content_type="text/html; charset=utf-8",
        size_bytes=9012,
        links_found=6,
        links_checked=6,
    ),
    PageRecord(
        run_id=42,
        url=f"{SITE}/admin/users",
        depth=2,
        crawlable=False,
        error="not crawled: https://example.com/robots.txt disallows this path",
    ),
]

LINKS = [
    LinkRecord(
        run_id=42,
        source_url=f"{SITE}/",
        target_url=f"{SITE}/shop/checkout",
        kind=LinkKind.INTERNAL,
        element="a",
        attribute="href",
        link_text="Checkout",
        final_url=f"{SITE}/shop/checkout",
        status=500,
        duration_ms=301,
        error=None,
        scheme="https",
        checked=True,
    ),
    LinkRecord(
        run_id=42,
        source_url=f"{SITE}/blog/post-1",
        target_url=f"{SITE}/docs/handbook.pdf",
        kind=LinkKind.INTERNAL,
        element="a",
        attribute="href",
        link_text="Handbook",
        status=404,
        duration_ms=97,
        scheme="https",
        checked=True,
    ),
    LinkRecord(
        run_id=42,
        source_url=f"{SITE}/about",
        target_url="https://cdn.example.test/widgets.js",
        kind=LinkKind.EXTERNAL,
        element="script",
        attribute="src",
        error="dns: not known",
        scheme="https",
        checked=True,
    ),
    LinkRecord(
        run_id=42,
        source_url=f"{SITE}/pricing",
        target_url="http://cdn.example.com/banner.png",
        kind=LinkKind.EXTERNAL,
        element="img",
        attribute="src",
        link_text="Banner",
        status=200,
        duration_ms=88,
        scheme="http",
        checked=True,
    ),
    LinkRecord(
        run_id=42,
        source_url=f"{SITE}/",
        target_url=f"{SITE}/mailto-nope",
        kind=LinkKind.INTERNAL,
        element="a",
        attribute="href",
        scheme="https",
        checked=False,
        error="excluded by pattern",
    ),
]

COUNTS = Counts(
    pages=6,
    links=48,
    checked=41,
    skipped_scheme=5,
    fragment_only=2,
    excluded=3,
    robots_blocked=1,
    errors=5,
    warnings=4,
    infos=3,
    by_type={
        "blocked_by_robots": 1,
        "broken": 2,
        "missing_fragment": 1,
        "mixed_content": 1,
        "redirect_chain": 1,
        "redirect_http_downgrade": 1,
        "redirect_loop": 1,
        "redirect_temporary_permanent_internal": 1,
        "redirect_to_error": 1,
        "slow": 1,
        "unreachable": 1,
    },
)


def sample_run(*, run_id: int = 42, status: RunStatus = RunStatus.COMPLETED) -> RunRecord:
    return RunRecord(
        id=run_id,
        site=SITE,
        started_at=STARTED,
        finished_at=FINISHED,
        status=status,
        seeds=[f"{SITE}/"],
        counts=COUNTS,
        config={
            "site": SITE,
            "user_agent": "Linkkeep/0.1.0 (+https://github.com/YOUR-OWNER/linkkeep)",
            "crawl": {"max_pages": 1000, "concurrency": 10, "rate": 5.0, "timeout": 15.0},
            "rules": {"include": [], "exclude": ["/admin/"], "ignore_status": [999]},
            "auth": {"headers": ["Authorization"], "cookies": [], "redacted": True},
        },
        notes="sitemap added 3 URL(s)",
    )


def sample_diff(new: list[Issue] | None = None) -> DiffResult:
    older = sample_run(run_id=41, status=RunStatus.COMPLETED)
    older = RunRecord(
        **{
            **older.model_dump(),
            "id": 41,
            "started_at": OLDER,
            "finished_at": OLDER + dt.timedelta(seconds=9),
        }
    )
    current = sample_run()
    added, removed, persisting = compare_issue_sets(
        [FIXED_LAST_WEEK, ALSO_FIXED, ISSUES[0]],
        list(ISSUES),
        first_seen={ISSUES[0].fingerprint: PERSISTING_SINCE},
        runs_seen={ISSUES[0].fingerprint: 4},
    )
    _ = (older, current)
    return DiffResult(
        site=SITE,
        run_a=older,
        run_b=current,
        new=new if new is not None else added,
        fixed=removed,
        persisting=[
            PersistingIssue(issue=item.issue, since=PERSISTING_SINCE, runs_seen=4)
            for item in persisting
        ],
    )


def sample_report_data(*, with_diff: bool = False, version: str = "0.1.0") -> ReportData:
    data = build_report_data(
        sample_run(),
        sorted(ISSUES, key=lambda issue: (*issue.sort_key(), issue.target_url)),
        PAGES,
        LINKS,
    )
    data.tool_version = version
    data.generated_at = STARTED + dt.timedelta(seconds=15)
    if with_diff:
        data.diff = sample_diff()
    return data
