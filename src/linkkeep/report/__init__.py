"""Report rendering: shared data shape and the format registry."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from linkkeep.config import ensure_parent
from linkkeep.diff import DiffResult
from linkkeep.models import Issue, IssueType, PageRecord, RunRecord, Severity, utcnow

JSON_SCHEMA_VERSION = 1


@dataclass
class ReportData:
    """Everything a renderer may show. Built from a crawl or from the database."""

    run: RunRecord
    issues: list[Issue] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)
    links: list[Any] = field(default_factory=list)
    diff: DiffResult | None = None
    generated_at: dt.datetime = field(default_factory=utcnow)
    tool_version: str = "0.0.0"

    @property
    def sorted_issues(self) -> list[Issue]:
        return sorted(self.issues, key=lambda issue: (*issue.sort_key(), issue.target_url))

    @property
    def issues_by_type(self) -> dict[IssueType, list[Issue]]:
        out: dict[IssueType, list[Issue]] = defaultdict(list)
        for issue in self.sorted_issues:
            out[issue.type].append(issue)
        return dict(sorted(out.items(), key=lambda item: _type_order(item[0])))

    def issues_by_source(self, wanted: Iterable[Issue]) -> dict[str | None, list[Issue]]:
        grouped: dict[str | None, list[Issue]] = defaultdict(list)
        for issue in wanted:
            grouped[issue.source_url].append(issue)
        return dict(sorted(grouped.items(), key=lambda item: (item[0] is None, item[0] or "")))

    @property
    def counts_by_severity(self) -> dict[Severity, int]:
        out = dict.fromkeys(Severity, 0)
        for issue in self.issues:
            out[issue.severity] += 1
        return out

    @property
    def duration_seconds(self) -> float:
        finished = self.run.finished_at or utcnow()
        return max(0.0, (finished - self.run.started_at).total_seconds())


def _type_order(issue_type: IssueType) -> tuple[int, str]:
    from linkkeep.models import SEVERITY_BY_TYPE, SEVERITY_ORDER

    return (SEVERITY_ORDER[SEVERITY_BY_TYPE[issue_type]], issue_type.value)


def build_report_data(
    run: RunRecord,
    issues: Sequence[Issue],
    pages: Sequence[PageRecord] = (),
    links: Sequence[Any] = (),
    *,
    diff: DiffResult | None = None,
) -> ReportData:
    from linkkeep import __version__

    return ReportData(
        run=run,
        issues=list(issues),
        pages=list(pages),
        links=list(links),
        diff=diff,
        tool_version=__version__,
    )


def from_storage(storage: Any, run_id: int, *, diff: DiffResult | None = None) -> ReportData:
    """Load a stored run (plus its issues/pages/links) for reporting."""
    run, issues, pages, links = storage.report_rows(run_id)
    return build_report_data(run, issues, pages, links, diff=diff)


def from_crawl(report: Any, *, diff: DiffResult | None = None) -> ReportData:
    """Build renderable data straight from a :class:`linkkeep.crawler.CrawlReport`."""
    return build_report_data(
        report.to_run_record(), report.issues, report.pages, report.links, diff=diff
    )


# --------------------------------------------------------------------------- output
def write_output(
    text: str, output: str | Path | None, *, console_print: Callable[[str], None] | None = None
) -> bool:
    """Write ``text`` to ``output`` (or hand it to ``console_print``). True when written."""
    if output is None or str(output) == "-":
        if console_print is not None:
            console_print(text)
        return False
    path = Path(str(output)).expanduser()
    ensure_parent(path)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    if console_print is not None:
        console_print(f"wrote {len(text.splitlines())} line(s) to {path}")
    return True


def render_text(data: ReportData) -> str:
    from linkkeep.report.text import render

    return render(data)


RENDERERS: dict[str, Callable[[ReportData], str]] = {}


def _register() -> None:  # pragma: no cover - import-time wiring
    from linkkeep.report import html as html_module
    from linkkeep.report import junit as junit_module
    from linkkeep.report import markdown as markdown_module
    from linkkeep.report import text as text_module
    from linkkeep.report import xjson as json_module

    RENDERERS.update(
        {
            "text": text_module.render,
            "markdown": markdown_module.render,
            "html": html_module.render,
            "json": json_module.render,
            "junit": junit_module.render,
        }
    )


def render(data: ReportData, fmt: str) -> str:
    """Render ``data`` in one of the supported formats."""
    if not RENDERERS:
        _register()
    renderer = RENDERERS.get(fmt)
    if renderer is None:
        msg = f"unknown report format: {fmt}"
        raise ValueError(msg)
    return renderer(data)


def json_data(data: ReportData) -> dict[str, Any]:
    from linkkeep.report.xjson import to_dict

    return to_dict(data)


# ----------------------------------------------------------------------- helpers
def stamp(value: dt.datetime | None, *, seconds: bool = True) -> str:
    """``2026-10-10 09:12 UTC`` (always UTC, always stable for golden files)."""
    if value is None:
        return "-"
    from linkkeep.models import to_utc

    moment = to_utc(value).astimezone(dt.timezone.utc)
    pattern = "%Y-%m-%d %H:%M" if seconds else "%Y-%m-%d"
    return moment.strftime(pattern)


def iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    from linkkeep.models import to_utc

    return to_utc(value).astimezone(dt.timezone.utc).isoformat()


def age_label(since: dt.datetime, *, now: dt.datetime | None = None) -> str:
    """``3 days`` / ``5 weeks`` - how long something has been broken."""
    from linkkeep.models import to_utc

    delta = (now or dt.datetime.now(dt.timezone.utc)) - to_utc(since)
    days = max(0, delta.days)
    if days == 0:
        return "today"
    if days == 1:
        return "1 day"
    if days < 14:
        return f"{days} days"
    if days < 120:
        return f"{days // 7} weeks"
    return f"{days // 30} months"


def shorten(value: str | None, limit: int = 78) -> str:
    if not value:
        return ""
    text = value if len(value) <= limit else value[: limit - 1].rstrip() + "\u2026"
    return " ".join(text.split())


def describe_issue(issue: Issue) -> str:
    """One line: ``HTTP 404`` / ``DNS failure: ...`` / ``5 redirects``."""
    if issue.status is not None:
        return f"HTTP {issue.status}"
    if issue.reason:
        return shorten(issue.reason, 90)
    return issue.description


PAGE_ELEMENT: str = "page"


def where_seen(issue: Issue) -> str:
    """``<a href> "Download"`` - the element that carried the link.

    Findings about a page itself (rather than a link on it) use the pseudo-element
    ``page``, which reads as ``the page itself`` instead of a made-up tag.
    """
    parts: list[str] = []
    if issue.element == PAGE_ELEMENT:
        parts.append("the page itself")
    elif issue.element:
        parts.append(
            f"<{issue.element} {issue.attribute}>" if issue.attribute else f"<{issue.element}>"
        )
    if issue.link_text:
        parts.append(f'"{shorten(issue.link_text, 40)}"')
    return " ".join(parts)


def since_text(since: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """``broken since 2026-03-01 (3 days)`` - the age is dropped when it adds nothing."""
    if since is None:
        return "first seen in this run"
    when = stamp(since, seconds=False)
    age = age_label(since, now=now)
    if age in {"today", "1 day"}:
        return f"broken since {when}"
    return f"broken since {when} ({age})"


def chain_summary(issue: Issue) -> str:
    """``chain: 301 a -> 302 b -> 200 c`` - only when something actually redirected."""
    if len(issue.redirect_chain) < 2:
        return ""
    steps = " -> ".join(
        f"{hop.status or '?'} {shorten(hop.url, 60)}" for hop in issue.redirect_chain
    )
    return f"chain: {steps}"


def summary_line(counts: dict[Severity, int], *, fail_on: str | None = None) -> str:
    parts = [
        f"{counts[Severity.ERROR]} error{'' if counts[Severity.ERROR] == 1 else 's'}",
        f"{counts[Severity.WARNING]} warning{'' if counts[Severity.WARNING] == 1 else 's'}",
        f"{counts[Severity.INFO]} info",
    ]
    line = ", ".join(parts)
    if fail_on is not None:
        line += f"  (fail-on={fail_on})"
    return line


def issue_table_rows(issues: Sequence[Issue], *, with_source: bool = True) -> list[str]:
    lines: list[str] = []
    for issue in issues:
        head = f"  {issue.code:<28} {describe_issue(issue):<28} {shorten(issue.target_url, 70)}"
        lines.append(head)
        if with_source and issue.source_url:
            lines.append(f"  {'':<28} {'':<28}   from {issue.source_url} ({where_seen(issue)})")
    return lines


__all__ = [
    "JSON_SCHEMA_VERSION",
    "RENDERERS",
    "ReportData",
    "from_crawl",
    "from_storage",
    "json_data",
    "render",
    "write_output",
]
