"""The async crawl engine.

The crawler performs no I/O of its own apart from the injected :class:`Fetcher` and the
optional :class:`~linkkeep.storage.Storage`; that is what makes the whole engine testable
with fakes.  Responsibilities:

* breadth-first walk over *internal* URLs from the seeds (depth/page limits, filters)
* status-only checks for *external* links, which are never crawled further
* politeness: robots.txt, ``Crawl-delay``, per-host rate limiting (owned by the fetcher)
* handing every response to :mod:`linkkeep.issues` and recording pages/links/issues

Link rows are finalised at the end of a run, so an internal link can report the status of
the page it pointed at without a second request.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linkkeep.config import Config, ConfigError
from linkkeep.extract import ExtractResult, extract_links, fragment_is_valid
from linkkeep.fetcher import Fetcher, FetchResult, HttpxFetcher
from linkkeep.issues import (
    DetectionSettings,
    LinkContext,
    detect_robots_block,
    run_link_detectors,
)
from linkkeep.models import (
    Counts,
    Issue,
    LinkKind,
    LinkRecord,
    PageRecord,
    RunRecord,
    RunStatus,
    Severity,
    utcnow,
)
from linkkeep.robots import RobotsClient, RobotsDecision
from linkkeep.urlnorm import (
    is_non_http_scheme,
    is_private_host,
    normalise_url,
    parse_url,
    same_site,
)

#: Pages between incremental database flushes, so an interrupted run stays useful.
FLUSH_EVERY = 25


@dataclass
class PageEntry:
    """One queued internal page."""

    url: str
    depth: int
    source_url: str | None = None


@dataclass
class LinkObservation:
    """A discovered link plus whatever we learned while checking it."""

    source_url: str
    target_url: str
    kind: LinkKind
    element: str | None = None
    attribute: str | None = None
    link_text: str | None = None
    fragment: str = ""
    depth: int = 0
    result: FetchResult | None = None
    checked: bool = True
    note: str | None = None


@dataclass
class CrawlReport:
    """Everything a run produced: input to storage and to every report renderer."""

    site: str
    seeds: list[str]
    status: RunStatus = RunStatus.COMPLETED
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    run_id: int | None = None
    pages: list[PageRecord] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)
    notes: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def issues_by_severity(self) -> dict[Severity, int]:
        out = dict.fromkeys(Severity, 0)
        for issue in self.issues:
            out[issue.severity] += 1
        return out

    def failing_issues(self, fail_on: str) -> list[Issue]:
        from linkkeep.issues import severity_at_least

        return [issue for issue in self.issues if severity_at_least(issue, fail_on)]

    def sorted_issues(self) -> list[Issue]:
        return sorted(self.issues, key=lambda issue: (*issue.sort_key(), issue.target_url))

    def to_run_record(self) -> RunRecord:
        return RunRecord(
            id=self.run_id or 0,
            site=self.site,
            started_at=self.started_at,
            finished_at=self.finished_at,
            status=self.status,
            seeds=list(self.seeds),
            counts=self.counts,
            config=self.config,
            notes="; ".join(self.notes) or None,
        )


def mixed_content_only(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """Run only the mixed-content rule (used for resources found on https pages)."""
    from linkkeep.issues import detect_mixed_content

    return detect_mixed_content(ctx, settings)


class Crawler:
    """Breadth-first site crawler with link checking."""

    def __init__(
        self,
        config: Config,
        fetcher: Fetcher,
        *,
        storage: Any | None = None,
        settings: DetectionSettings | None = None,
        interrupt: asyncio.Event | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.fetcher = fetcher
        self.storage = storage
        self.settings = settings or settings_from_config(config)
        self.interrupt = interrupt if interrupt is not None else asyncio.Event()
        self._log = log if log is not None else _silent
        self._clock = clock
        self._filter = config.url_filter()
        self._robots = RobotsClient(
            fetcher, config.user_agent, enabled=not config.rules.ignore_robots
        )
        self._visited: set[str] = set()
        self._queued: set[str] = set()
        self._pending: deque[PageEntry] = deque()
        self._inflight = 0
        self._observations: list[LinkObservation] = []
        self._records: list[LinkRecord] = []
        self._issues: dict[str, Issue] = {}
        self._pages: dict[str, PageRecord] = {}
        self._anchors: dict[str, set[str]] = {}
        self._page_results: dict[str, FetchResult] = {}
        self._external_results: dict[str, FetchResult] = {}
        self._external_locks: dict[str, asyncio.Lock] = {}
        self._fragment_checks: list[LinkObservation] = []
        self._links_checked_per_page: dict[str, int] = {}
        self._notes: list[str] = []
        self._flags: set[str] = set()
        self._fragments_checked = False
        self._ok_pages = 0
        self._claimed = 0
        self._network_errors = 0
        self._cancelled = False
        self.counts = Counts()
        self._run_id: int | None = None
        self._started = 0.0
        self._flushed = 0

    # ------------------------------------------------------------------ public
    async def run(self, seeds: Sequence[str], *, site: str | None = None) -> CrawlReport:
        """Crawl every seed and return the report (never raises for network failures)."""
        urls = prepare_seeds(seeds)
        self._check_seeds_are_allowed(urls)
        if isinstance(self.fetcher, HttpxFetcher):
            self.fetcher.allow_private = self.config.rules.allow_private
            self.fetcher.set_auth_origins(urls)
        report = CrawlReport(
            site=site or urls[0],
            seeds=list(urls),
            started_at=utcnow(),
            config=self.config.to_stored_dict(),
        )
        self._started = self._clock()
        self._create_run(report)
        for url in urls:
            self._enqueue(PageEntry(url=url, depth=0))
        await self._load_sitemap_seeds(urls)
        try:
            await self._drain()
        finally:
            report.notes = list(self._notes)
            report.status = RunStatus.INTERRUPTED if self._cancelled else RunStatus.COMPLETED
            if self._ok_pages == 0 and self._network_errors > 0:
                report.status = RunStatus.FAILED
                report.notes.append("no page could be fetched, so the crawl did not run")
            await self._finish(report)
        return report

    # ------------------------------------------------------------- queue logic
    def _check_seeds_are_allowed(self, urls: Sequence[str]) -> None:
        if self.config.rules.allow_private:
            return
        for url in urls:
            parsed = parse_url(url)
            if parsed is not None and is_private_host(parsed.host):
                msg = (
                    f"seed {url} resolves to a private or loopback address. "
                    "Linkkeep refuses that when the target is not public; pass --allow-private "
                    "if you really mean it (local development, CI)."
                )
                raise ConfigError(msg)

    def _enqueue(self, entry: PageEntry) -> bool:
        url = entry.url
        if url in self._visited or url in self._queued:
            return False
        budget = self.config.crawl.max_pages
        if self._claimed >= budget:
            self._flag("stopped at max_pages=" + str(budget))
            return False
        self._claimed += 1
        self._queued.add(url)
        self._pending.append(entry)
        return True

    async def _drain(self) -> None:
        """Run workers until nothing is pending, or until interrupted."""
        limit = self.config.crawl.concurrency
        queue: asyncio.Queue[PageEntry | None] = asyncio.Queue()
        workers = [asyncio.create_task(self._worker(queue)) for _ in range(limit)]
        try:
            while not self._cancelled:
                self._pump(queue)
                if self._inflight == 0 and not self._pending:
                    break
                if self.interrupt.is_set():
                    self._cancelled = True
                    self._flag("interrupted by user; partial results saved")
                    break
                if await self._join_or_interrupt(queue, workers):
                    break
        finally:
            for _ in workers:
                queue.put_nowait(None)
            await self._reap(workers)

    def _pump(self, queue: asyncio.Queue[PageEntry | None]) -> None:
        while self._pending:
            entry = self._pending.popleft()
            self._queued.discard(entry.url)
            if entry.url in self._visited:
                continue
            self._visited.add(entry.url)
            self._inflight += 1
            queue.put_nowait(entry)

    async def _join_or_interrupt(
        self, queue: asyncio.Queue[PageEntry | None], workers: list[Any]
    ) -> bool:
        """Wait for the in-flight pages; True when the wait was cut short by an interrupt."""
        joiner = asyncio.create_task(queue.join())
        waiter = asyncio.create_task(self.interrupt.wait())
        done, _pending = await asyncio.wait({joiner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done and joiner not in done:
            joiner.cancel()
            await asyncio.gather(joiner, return_exceptions=True)
            self._cancelled = True
            self._flag("interrupted by user; partial results saved")
            await self._cancel_workers(workers)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        return self._cancelled

    async def _reap(self, workers: list[Any]) -> None:
        await asyncio.gather(*workers, return_exceptions=True)

    async def _cancel_workers(self, workers: list[Any]) -> None:
        for task in workers:
            task.cancel()
        await self._reap(workers)

    async def _worker(self, queue: asyncio.Queue[PageEntry | None]) -> None:
        while True:
            entry = await queue.get()
            try:
                if entry is None:
                    return
                await self._process_page(entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # defensive: one bad page must not kill the crawl
                self._notes.append(f"error while processing {getattr(entry, 'url', '?')}: {exc!r}")
            finally:
                queue.task_done()

    # ------------------------------------------------------------- page engine
    async def _process_page(self, entry: PageEntry) -> None:
        try:
            await self._handle_page(entry)
        finally:
            self._inflight -= 1
            if self.counts.pages >= self._flushed + FLUSH_EVERY:
                await self._flush()

    async def _handle_page(self, entry: PageEntry) -> None:
        url = entry.url
        decision = await self._robots_decision(url)
        if not decision.allowed:
            self._record_page(
                PageRecord(
                    run_id=self._run_id or 0,
                    url=url,
                    depth=entry.depth,
                    crawlable=False,
                    error=f"not crawled: {decision.robots_url} disallows this path",
                )
            )
            self.counts.robots_blocked += 1
            self._add_issues(
                [
                    detect_robots_block(
                        source_url=entry.source_url, target_url=url, element="page", link_text=None
                    )
                ]
            )
            return
        await self._respect_crawl_delay(url)
        result = await self.fetcher.fetch(url, method="GET", want_body=True)
        self.counts.pages += 1
        self._page_results[url] = result
        if result.error is not None:
            self._network_errors += 1
        if result.status is not None and result.status < 400:
            self._ok_pages += 1
        page = PageRecord(
            run_id=self._run_id or 0,
            url=url,
            depth=entry.depth,
            status=result.status,
            final_url=result.final_url,
            duration_ms=result.duration_ms,
            content_type=result.content_type,
            size_bytes=result.size_bytes,
            error=result.error_reason or None,
        )
        self._record_page(page)
        self._add_issues(
            run_link_detectors(
                LinkContext(
                    target_url=url,
                    result=result,
                    internal=True,
                    source_url=entry.source_url,
                    element="page",
                    link_text=None,
                ),
                self.settings,
            )
        )
        html = result.html_body
        if html is None:
            if result.truncated:
                page.error = "body larger than max_response_size; links not extracted"
            elif result.error is None and result.status is not None and not result.is_html:
                page.error = (
                    f"not HTML ({result.content_type or 'unknown type'}); links not extracted"
                )
            return
        await self._handle_links(url, entry.depth, html, result)

    async def _handle_links(
        self, page_url: str, depth: int, html: str, page_result: FetchResult
    ) -> None:
        extracted: ExtractResult = extract_links(
            html,
            page_result.final_url or page_url,
            sort_query=self.config.rules.url_sort_query,
            slash_policy=self.config.rules.url_trailing_slash,
            include_fragments=self.config.rules.check_fragments,
        )
        self._anchors[canonical_target(page_result.final_url or page_url)] = extracted.anchors
        page = self._pages.get(page_url)
        if page is not None:
            page.links_found = len(extracted.links)
        self.counts.skipped_scheme += extracted.skipped.get("scheme", 0)
        self.counts.fragment_only += extracted.skipped.get("fragment-only", 0)
        page_scheme = scheme_of(page_result.final_url or page_url)
        for link in extracted.links:
            await self._handle_single_link(page_url, depth, page_scheme, link, extracted)
        if page is not None:
            page.links_checked = self._links_checked_per_page.get(page_url, 0)

    async def _handle_single_link(
        self, page_url: str, depth: int, page_scheme: str, link: Any, extracted: ExtractResult
    ) -> None:
        del extracted  # counted above; kept in the signature for readability of callers
        target = link.url
        internal = same_site(
            page_url, target, include_subdomains=self.config.rules.include_subdomains
        )
        if internal and target == page_url and not link.fragment:
            return
        observation = LinkObservation(
            source_url=page_url,
            target_url=target,
            kind=LinkKind.INTERNAL if internal else LinkKind.EXTERNAL,
            element=link.element,
            attribute=link.attribute,
            link_text=link.link_text,
            fragment=link.fragment,
            depth=depth + 1,
        )
        if not self._filter.allows(target):
            self.counts.excluded += 1
            observation.checked = False
            observation.note = "excluded by pattern"
            self._observations.append(observation)
            return
        if link.is_resource and page_scheme == "https" and scheme_of(target) == "http":
            self._add_issues(
                run_link_detectors(
                    LinkContext(
                        target_url=target,
                        result=_pending_result(target),
                        internal=internal,
                        source_url=page_url,
                        element=link.element,
                        attribute=link.attribute,
                        link_text=link.link_text,
                    ),
                    self.settings,
                    detectors=[mixed_content_only],
                )
            )
        if self._is_private(target):
            # Never request a private/loopback address from a public crawl, whether the
            # link looks internal or external (a link is attacker- or CMS-controlled).
            self.counts.excluded += 1
            observation.checked = False
            observation.note = "skipped: private or loopback address (use --allow-private)"
            self._observations.append(observation)
            return
        if link.fragment and self.config.rules.check_fragments:
            self._fragment_checks.append(observation)
        if internal:
            await self._handle_internal_link(page_url, depth, observation)
            return
        await self._handle_external_link(observation)

    async def _handle_internal_link(
        self, page_url: str, depth: int, observation: LinkObservation
    ) -> None:
        decision = await self._robots_decision(observation.target_url)
        if not decision.allowed:
            self.counts.robots_blocked += 1
            observation.checked = False
            observation.note = f"blocked by robots.txt ({decision.robots_url})"
            self._observations.append(observation)
            self._add_issues(
                [
                    detect_robots_block(
                        source_url=page_url,
                        target_url=observation.target_url,
                        element=observation.element,
                        link_text=observation.link_text,
                    )
                ]
            )
            return
        self._observations.append(observation)
        self._links_checked_per_page[page_url] = self._links_checked_per_page.get(page_url, 0) + 1
        max_depth = self.config.crawl.max_depth
        if max_depth is not None and depth + 1 > max_depth:
            self.counts.excluded += 1
            observation.checked = False
            observation.note = f"not crawled: deeper than max_depth={max_depth}"
            return
        self._enqueue(PageEntry(url=observation.target_url, depth=depth + 1, source_url=page_url))

    async def _handle_external_link(self, observation: LinkObservation) -> None:
        if not self.config.rules.follow_external or self.interrupt.is_set():
            self.counts.excluded += 1
            observation.checked = False
            observation.note = (
                "external links not checked (--no-external)"
                if not self.config.rules.follow_external
                else "not checked: interrupted"
            )
            self._observations.append(observation)
            return
        decision = await self._robots_decision(observation.target_url)
        if not decision.allowed:
            self.counts.robots_blocked += 1
            observation.checked = False
            observation.note = "blocked by robots.txt"
            self._observations.append(observation)
            self._add_issues(
                [
                    detect_robots_block(
                        source_url=observation.source_url,
                        target_url=observation.target_url,
                        element=observation.element,
                        link_text=observation.link_text,
                    )
                ]
            )
            return
        await self._respect_crawl_delay(observation.target_url)
        result = await self._check_external(observation.target_url)
        observation.result = result
        self._links_checked_per_page[observation.source_url] = (
            self._links_checked_per_page.get(observation.source_url, 0) + 1
        )
        self._observations.append(observation)
        self._add_issues(
            run_link_detectors(
                LinkContext(
                    target_url=observation.target_url,
                    result=result,
                    internal=False,
                    source_url=observation.source_url,
                    element=observation.element,
                    attribute=observation.attribute,
                    link_text=observation.link_text,
                ),
                self.settings,
            )
        )

    async def _check_external(self, url: str) -> FetchResult:
        lock = self._external_locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._external_results.get(url)
            if cached is None:
                cached = await self.fetcher.check_link(url)
                self._external_results[url] = cached
            return cached

    def _is_private(self, url: str) -> bool:
        if self.config.rules.allow_private:
            return False
        parsed = parse_url(url)
        return bool(parsed is not None and is_private_host(parsed.host))

    async def _robots_decision(self, url: str) -> RobotsDecision:
        if self.config.rules.ignore_robots:
            return RobotsDecision(True, "", "disabled", None)
        return await self._robots.decision(url)

    async def _respect_crawl_delay(self, url: str) -> None:
        if not self.config.respect_crawl_delay or self.config.rules.ignore_robots:
            return
        delay = await self._robots.crawl_delay(url)
        if delay:
            self._log(f"waiting for robots.txt Crawl-delay: {delay}s")
            limiter = getattr(self.fetcher, "limiter", None)
            parsed = parse_url(url)
            if limiter is not None and parsed is not None:
                limiter.set_delay(parsed.host, float(delay))
            else:
                await asyncio.sleep(float(delay))

    def _flag(self, message: str) -> None:
        if message not in self._flags:
            self._flags.add(message)
            self._notes.append(message)

    # ---------------------------------------------------------------- sitemap
    async def _load_sitemap_seeds(self, seeds: Sequence[str]) -> None:
        if not self.config.rules.sitemap or not seeds:
            return
        added = 0
        seen: set[str] = set()
        for seed in seeds:
            for sitemap_url in await self._discover_sitemaps(seed):
                for candidate in await self._read_sitemap(sitemap_url, depth=0):
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if not same_site(
                        seed, candidate, include_subdomains=self.config.rules.include_subdomains
                    ):
                        continue
                    if self._enqueue(PageEntry(url=candidate, depth=0, source_url=None)):
                        added += 1
        self._flag(f"sitemap added {added} URL(s)")
        self._log(f"sitemap: queued {added} extra URL(s)")

    async def _discover_sitemaps(self, seed: str) -> list[str]:
        parsed = parse_url(seed)
        if parsed is None:
            return []
        origin = parsed.origin
        candidates = [f"{origin}/sitemap.xml"]
        for declared in await self._robots.sitemaps(seed):
            if declared not in candidates:
                candidates.append(declared)
        return candidates

    async def _read_sitemap(self, url: str, *, depth: int) -> list[str]:
        """Read a sitemap (or one level of sitemap index) and return normalised URLs."""
        if self._is_private(url) or not self._filter.allows(url):
            return []
        result = await self.fetcher.fetch(url, method="GET", want_body=True)
        if (
            result.error is not None
            or result.status is None
            or result.status >= 400
            or not result.body
        ):
            self._log(f"sitemap: could not read {url}")
            return []
        body = result.body.decode("utf-8", errors="replace")
        is_index = "<sitemap>" in body.lower()
        out: list[str] = []
        for match in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", body):
            normalised = normalise_url(match.group(1))
            if normalised is None:
                continue
            if is_index and depth < 1:
                out.extend(await self._read_sitemap(normalised, depth=depth + 1))
            else:
                out.append(normalised)
        return out

    # ------------------------------------------------------------- recording
    def _record_page(self, page: PageRecord) -> None:
        self._pages[page.url] = page

    def _add_issues(self, issues: Iterable[Issue]) -> None:
        for issue in issues:
            self._issues.setdefault(issue.fingerprint, issue)

    def _create_run(self, report: CrawlReport) -> None:
        if self.storage is None:
            return
        self._run_id = self.storage.create_run(
            report.site, report.seeds, report.config, report.started_at
        )
        report.run_id = self._run_id

    async def _flush(self) -> None:
        if self.storage is None or self._run_id is None:
            return
        self._finalise_links()
        self._flushed = self.counts.pages
        await asyncio.to_thread(
            self.storage.write_from_thread,
            self.storage.save_run_data,
            self._run_id,
            list(self._pages.values()),
            list(self._records),
            list(self._issues.values()),
            self._counts(),
            RunStatus.RUNNING,
            None,
            list(self._notes),
        )

    async def _finish(self, report: CrawlReport) -> None:
        self._finalise_links()
        self._run_fragment_checks()
        report.pages = list(self._pages.values())
        report.links = list(self._records)
        report.issues = sorted(
            self._issues.values(), key=lambda issue: (*issue.sort_key(), issue.target_url)
        )
        report.counts = self._counts()
        report.finished_at = utcnow()
        report.duration_seconds = max(0.0, self._clock() - self._started)
        if self._cancelled:
            report.notes.append("interrupted")
        if self.storage is not None and self._run_id is not None:
            await asyncio.to_thread(
                self.storage.write_from_thread,
                self.storage.save_run_data,
                self._run_id,
                report.pages,
                report.links,
                report.issues,
                report.counts,
                report.status,
                report.finished_at,
                report.notes,
            )
            report.run_id = self._run_id

    def _counts(self) -> Counts:
        counts = self.counts.model_copy()
        issues = list(self._issues.values())
        counts.errors = sum(1 for issue in issues if issue.severity is Severity.ERROR)
        counts.warnings = sum(1 for issue in issues if issue.severity is Severity.WARNING)
        counts.infos = sum(1 for issue in issues if issue.severity is Severity.INFO)
        counts.links = len(self._observations)
        counts.checked = sum(1 for record in self._records if record.checked)
        by_type: dict[str, int] = {}
        for issue in issues:
            by_type[issue.code] = by_type.get(issue.code, 0) + 1
        counts.by_type = dict(sorted(by_type.items()))
        return counts

    def _finalise_links(self) -> None:
        """Turn observations into rows, filling internal links from the page results."""
        records: list[LinkRecord] = []
        for observation in self._observations:
            result = observation.result
            if observation.kind is LinkKind.INTERNAL:
                result = self._page_results.get(observation.target_url, observation.result)
            checked = observation.checked and result is not None
            status = result.status if result is not None else None
            final_url = result.final_url if result is not None and result.final_url else None
            error = result.error_reason if result is not None else None
            records.append(
                LinkRecord(
                    run_id=self._run_id or 0,
                    source_url=observation.source_url,
                    target_url=observation.target_url,
                    kind=observation.kind,
                    element=observation.element,
                    attribute=observation.attribute,
                    link_text=observation.link_text,
                    final_url=final_url,
                    status=status,
                    duration_ms=result.duration_ms if result is not None else None,
                    error=observation.note or (error or None),
                    scheme=scheme_of(observation.target_url) or "https",
                    checked=checked,
                    redirect_chain=list(result.chain) if result is not None else [],
                )
            )
        self._records = records

    def _run_fragment_checks(self) -> None:
        """Report ``missing_fragment`` once every crawled page has its anchor set."""
        if self._fragments_checked or not self.config.rules.check_fragments:
            return
        self._fragments_checked = True
        from linkkeep.issues import detect_missing_fragment

        for observation in self._fragment_checks:
            fragment = observation.fragment
            if not fragment or not fragment_is_valid(fragment):
                continue
            page_result = self._page_results.get(observation.target_url)
            if (
                page_result is None
                or page_result.status is None
                or not 200 <= page_result.status < 300
            ):
                continue
            anchors = self._anchors.get(
                canonical_target(page_result.final_url or observation.target_url)
            )
            issue = detect_missing_fragment(
                source_url=observation.source_url,
                target_url=observation.target_url,
                fragment=fragment,
                anchors=anchors,
                element=observation.element,
                attribute=observation.attribute,
                link_text=observation.link_text,
            )
            if issue is not None:
                self._add_issues([issue])


# --------------------------------------------------------------------------- helpers
def settings_from_config(config: Config) -> DetectionSettings:
    return DetectionSettings(
        slow_ms=config.detection.slow_ms,
        max_redirect_hops=config.crawl.max_redirect_hops,
        ignore_status=frozenset(config.rules.ignore_status),
        report_temporary_redirects=config.detection.temporary_redirect_hint,
        report_mixed_content=config.detection.mixed_content,
    )


def prepare_seeds(seeds: Iterable[str]) -> list[str]:
    """Normalise user-supplied seeds, adding ``https://`` when no scheme was given."""
    out: list[str] = []
    for seed in seeds:
        text = str(seed).strip()
        if not text:
            continue
        if "://" not in text:
            text = f"https://{text}"
        normalised = normalise_url(text)
        if normalised is None or parse_url(normalised) is None:
            msg = f"not a valid URL: {seed!r}"
            raise ConfigError(msg)
        out.append(normalised)
    seen: set[str] = set()
    unique: list[str] = []
    for url in out:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    if not unique:
        msg = "no seed URL given (pass one on the command line or set `site:` in the config)"
        raise ConfigError(msg)
    return unique


def canonical_target(url: str) -> str:
    return normalise_url(url) or url


def scheme_of(url: str) -> str:
    parsed = parse_url(url)
    return parsed.scheme if parsed is not None else ""


def _pending_result(url: str) -> FetchResult:
    """A "not checked yet" result, used when a rule does not need the response."""
    return FetchResult(url=url, method="GET", status=None, final_url=url)


def _silent(_message: str) -> None:
    return None


def ignored_scheme(url: str) -> str | None:
    """Scheme name for links we never fetch (``mailto``, ``tel``, ...)."""
    if not is_non_http_scheme(url):
        return None
    return url.split(":", 1)[0].lower()


__all__ = [
    "CrawlReport",
    "Crawler",
    "LinkObservation",
    "PageEntry",
    "ignored_scheme",
    "prepare_seeds",
    "settings_from_config",
]
