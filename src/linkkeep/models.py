"""Domain models shared by the crawler, storage and reports.

Everything here is plain data: no I/O, no httpx, no sqlite.  The pydantic
models are used both for validation (config) and as the row objects that move
between the crawler, the database and the report renderers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    """How much an issue should bother you."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueType(str, Enum):
    """Stable machine-readable issue codes.

    The values are part of Linkkeep's public API (they appear in JSON reports
    and in the database) and must never be renamed, only added to.
    """

    BROKEN = "broken"
    UNREACHABLE = "unreachable"
    REDIRECT_CHAIN = "redirect_chain"
    REDIRECT_LOOP = "redirect_loop"
    REDIRECT_TO_ERROR = "redirect_to_error"
    REDIRECT_HTTP_DOWNGRADE = "redirect_http_downgrade"
    REDIRECT_TEMPORARY_PERMANENT_INTERNAL = "redirect_temporary_permanent_internal"
    MIXED_CONTENT = "mixed_content"
    SLOW = "slow"
    MISSING_FRAGMENT = "missing_fragment"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"


SEVERITY_BY_TYPE: dict[IssueType, Severity] = {
    IssueType.BROKEN: Severity.ERROR,
    IssueType.UNREACHABLE: Severity.ERROR,
    IssueType.REDIRECT_CHAIN: Severity.WARNING,
    IssueType.REDIRECT_LOOP: Severity.ERROR,
    IssueType.REDIRECT_TO_ERROR: Severity.ERROR,
    IssueType.REDIRECT_HTTP_DOWNGRADE: Severity.WARNING,
    IssueType.REDIRECT_TEMPORARY_PERMANENT_INTERNAL: Severity.INFO,
    IssueType.MIXED_CONTENT: Severity.WARNING,
    IssueType.SLOW: Severity.INFO,
    IssueType.MISSING_FRAGMENT: Severity.WARNING,
    IssueType.BLOCKED_BY_ROBOTS: Severity.INFO,
}

#: Human readable one-liners used by every report format.
DESCRIPTION_BY_TYPE: dict[IssueType, str] = {
    IssueType.BROKEN: "Target responded with a 4xx/5xx status",
    IssueType.UNREACHABLE: "Target could not be reached at all",
    IssueType.REDIRECT_CHAIN: "Too many redirects in the chain",
    IssueType.REDIRECT_LOOP: "Redirect chain loops back to a URL already seen",
    IssueType.REDIRECT_TO_ERROR: "Redirect chain ends in a 4xx/5xx status",
    IssueType.REDIRECT_HTTP_DOWNGRADE: "HTTPS URL redirects down to HTTP",
    IssueType.REDIRECT_TEMPORARY_PERMANENT_INTERNAL: "Internal link uses a temporary redirect",
    IssueType.MIXED_CONTENT: "HTTP resource linked from an HTTPS page",
    IssueType.SLOW: "Target is slower than the configured threshold",
    IssueType.MISSING_FRAGMENT: "The #fragment does not exist on the target page",
    IssueType.BLOCKED_BY_ROBOTS: "robots.txt prevented Linkkeep from crawling this URL",
}

SEVERITY_ORDER: dict[Severity, int] = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

#: Ordering used by every report so output is stable and diffable.
SEVERITY_RANK = SEVERITY_ORDER


class RedirectHop(BaseModel):
    """One step of a manually-followed redirect chain."""

    model_config = ConfigDict(frozen=True)

    url: str
    status: int | None = None
    location: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "status": self.status, "location": self.location}


def make_fingerprint(issue_type: IssueType | str, source_url: str | None, target_url: str) -> str:
    """Stable identity of a problem across runs.

    Only ``type + source + target`` take part so that a link stays "the same
    problem" when its status code or final URL changes.
    """
    kind = issue_type.value if isinstance(issue_type, IssueType) else str(issue_type)
    payload = f"{kind}\n{(source_url or '').rstrip()}\n{(target_url or '').rstrip()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class Issue(BaseModel):
    """A single detected problem."""

    model_config = ConfigDict(validate_assignment=True)

    type: IssueType
    fingerprint: str = Field(min_length=8)
    source_url: str | None = None
    target_url: str
    final_url: str | None = None
    status: int | None = None
    reason: str | None = None
    element: str | None = None
    attribute: str | None = None
    link_text: str | None = None
    redirect_chain: list[RedirectHop] = Field(default_factory=list)
    found_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def code(self) -> str:
        return self.type.value

    @property
    def severity(self) -> Severity:
        return SEVERITY_BY_TYPE[self.type]

    @property
    def description(self) -> str:
        return DESCRIPTION_BY_TYPE[self.type]

    @property
    def hops(self) -> int:
        return len(self.redirect_chain)

    @property
    def label(self) -> str:
        """Short, report-friendly wording including the concrete detail."""
        if self.status is not None:
            return f"HTTP {self.status}"
        if self.reason:
            return self.reason
        if self.type is IssueType.MISSING_FRAGMENT and self.final_url:
            return "fragment missing on target page"
        return self.description

    def sort_key(self) -> tuple[int, str, str, str]:
        return (SEVERITY_ORDER[self.severity], self.code, self.source_url or "", self.target_url)

    def public_dict(self) -> dict[str, Any]:
        """JSON-report shape (stable keys, ISO-8601 timestamps)."""
        return {
            "code": self.code,
            "severity": self.severity.value,
            "fingerprint": self.fingerprint,
            "source_url": self.source_url,
            "target_url": self.target_url,
            "final_url": self.final_url,
            "status": self.status,
            "reason": self.reason,
            "element": self.element,
            "attribute": self.attribute,
            "link_text": self.link_text,
            "redirect_chain": [hop.as_dict() for hop in self.redirect_chain],
            "found_at": self.found_at.isoformat(),
        }


class LinkKind(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class PageRecord(BaseModel):
    """One crawled page of a run."""

    run_id: int
    url: str
    depth: int = 0
    status: int | None = None
    final_url: str | None = None
    duration_ms: int | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    error: str | None = None
    crawlable: bool = True
    links_found: int = 0
    links_checked: int = 0


class LinkRecord(BaseModel):
    """One discovered link, whether or not it was checked."""

    run_id: int
    source_url: str
    target_url: str
    kind: LinkKind = LinkKind.INTERNAL
    element: str | None = None
    attribute: str | None = None
    link_text: str | None = None
    final_url: str | None = None
    status: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    scheme: str = "https"
    checked: bool = True
    redirect_chain: list[RedirectHop] = Field(default_factory=list)


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class Counts(BaseModel):
    """Counters kept on the run row so ``linkkeep runs`` needs no joins."""

    pages: int = 0
    links: int = 0
    checked: int = 0
    skipped_scheme: int = 0
    fragment_only: int = 0
    excluded: int = 0
    robots_blocked: int = 0
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)

    @property
    def total_issues(self) -> int:
        return self.errors + self.warnings + self.infos


class RunRecord(BaseModel):
    """A crawl, as stored in the database."""

    id: int
    site: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    seeds: list[str] = Field(default_factory=list)
    counts: Counts = Field(default_factory=Counts)
    config: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_utc(value: dt.datetime) -> dt.datetime:
    """Normalise a timestamp to UTC (sqlite hands back naive datetimes)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)
