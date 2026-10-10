"""Issue detection rules.

Every detector is a pure function that takes already-fetched facts and returns
:class:`~linkkeep.models.Issue` objects - no I/O, no network, easy to test.

**How to add a detector** (also documented in ``CONTRIBUTING.md``):

1. Add a member to :class:`linkkeep.models.IssueType` with a stable, snake_case value.
2. Give it a severity in ``SEVERITY_BY_TYPE`` and a sentence in ``DESCRIPTION_BY_TYPE``.
3. Write the rule as a small pure function in this file returning ``list[Issue]`` and
   register it in :data:`DETECTORS` (link-level rules) or call it from the crawler
   (page-level rules).
4. Add a unit test in ``tests/test_issues.py``, a row to the issue table in ``README.md``
   and (if the format output changes) regenerate the report golden files with
   ``make goldens``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from linkkeep.fetcher import (
    ERROR_BAD_LOCATION,
    ERROR_LOOP,
    ERROR_TOO_LARGE,
    ERROR_TOO_MANY_HOPS,
    REDIRECT_STATUSES,
    FetchResult,
)
from linkkeep.models import Issue, IssueType, RedirectHop, Severity, make_fingerprint

TEMPORARY_STATUSES: frozenset[int] = frozenset({302, 307})

ERROR_LABELS: dict[str, str] = {
    "dns": "DNS failure",
    "connect": "connection refused",
    "tls": "TLS error",
    "timeout": "timed out",
    "redirect_loop": "redirect loop",
    "redirect_limit": "too many redirects",
    "redirect_without_location": "redirect without Location header",
    "too_large": "response too large to parse",
    "error": "request failed",
}


@dataclass(frozen=True)
class DetectionSettings:
    """Tunables a detector may consult."""

    slow_ms: int = 5000
    max_redirect_hops: int = 3
    ignore_status: frozenset[int] = field(default_factory=frozenset)
    report_temporary_redirects: bool = True
    report_mixed_content: bool = True


@dataclass(frozen=True)
class LinkContext:
    """One link, the page it came from, and what happened when we checked it."""

    target_url: str
    result: FetchResult
    internal: bool = True
    source_url: str | None = None
    element: str | None = None
    attribute: str | None = None
    link_text: str | None = None
    fragment: str = ""

    @property
    def chain(self) -> list[RedirectHop]:
        return self.result.chain

    @property
    def redirects(self) -> list[RedirectHop]:
        return [hop for hop in self.result.chain if hop.status in REDIRECT_STATUSES]


def build_issue(
    issue_type: IssueType,
    *,
    source_url: str | None,
    target_url: str,
    final_url: str | None = None,
    status: int | None = None,
    reason: str | None = None,
    element: str | None = None,
    attribute: str | None = None,
    link_text: str | None = None,
    chain: Sequence[RedirectHop] = (),
) -> Issue:
    """Create an issue whose fingerprint comes from ``type + source + target``."""
    return Issue(
        type=issue_type,
        fingerprint=make_fingerprint(issue_type, source_url, target_url),
        source_url=source_url,
        target_url=target_url,
        final_url=final_url,
        status=status,
        reason=reason,
        element=element,
        attribute=attribute,
        link_text=link_text,
        redirect_chain=list(chain),
    )


def error_reason(kind: str, detail: str) -> str:
    """Short, stable reason text (exception strings are squashed and truncated)."""
    label = ERROR_LABELS.get(kind, kind)
    cleaned = " ".join(detail.split())
    if len(cleaned) > 160:
        cleaned = cleaned[:159].rstrip() + "\u2026"
    if not cleaned or cleaned.startswith(label):
        return label
    return f"{label}: {cleaned}"


def detect_unreachable(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """DNS/connection/TLS/timeout failures, plus redirect chains that never resolve."""
    error = ctx.result.error
    if error is None:
        return []
    if error.kind == ERROR_TOO_LARGE:
        # We simply did not parse the page; that is not a link problem.
        return []
    issue_type = IssueType.UNREACHABLE
    if error.kind == ERROR_LOOP:
        issue_type = IssueType.REDIRECT_LOOP
    elif error.kind in {ERROR_TOO_MANY_HOPS, ERROR_BAD_LOCATION}:
        issue_type = IssueType.REDIRECT_CHAIN
    return [
        build_issue(
            issue_type,
            source_url=ctx.source_url,
            target_url=ctx.target_url,
            final_url=ctx.result.final_url,
            status=ctx.result.status,
            reason=error_reason(error.kind, error.detail),
            element=ctx.element,
            attribute=ctx.attribute,
            link_text=ctx.link_text,
            chain=ctx.result.chain,
        )
    ]


def detect_broken(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """4xx/5xx responses, distinguishing "redirect that lands on an error"."""
    status = ctx.result.status
    if (
        status is None
        or status < 400
        or status in settings.ignore_status
        or ctx.result.error is not None
    ):
        return []
    if ctx.redirects:
        return [
            build_issue(
                IssueType.REDIRECT_TO_ERROR,
                source_url=ctx.source_url,
                target_url=ctx.target_url,
                final_url=ctx.result.final_url,
                status=status,
                reason=f"redirect chain ends in HTTP {status}",
                element=ctx.element,
                attribute=ctx.attribute,
                link_text=ctx.link_text,
                chain=ctx.result.chain,
            )
        ]
    return [
        build_issue(
            IssueType.BROKEN,
            source_url=ctx.source_url,
            target_url=ctx.target_url,
            final_url=ctx.result.final_url,
            status=status,
            element=ctx.element,
            attribute=ctx.attribute,
            link_text=ctx.link_text,
            chain=ctx.result.chain,
        )
    ]


def detect_redirect_problems(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """Long chains, HTTPS->HTTP downgrades and temporary redirects on internal links."""
    if ctx.result.error is not None:
        return []
    issues: list[Issue] = []
    redirects = ctx.redirects
    hop_count = len(redirects)
    if hop_count > settings.max_redirect_hops:
        issues.append(
            build_issue(
                IssueType.REDIRECT_CHAIN,
                source_url=ctx.source_url,
                target_url=ctx.target_url,
                final_url=ctx.result.final_url,
                status=ctx.result.status,
                reason=f"{hop_count} redirects, more than the limit of {settings.max_redirect_hops}",
                element=ctx.element,
                attribute=ctx.attribute,
                link_text=ctx.link_text,
                chain=ctx.result.chain,
            )
        )
    if (
        hop_count
        and _scheme_of(ctx.target_url) == "https"
        and _scheme_of(ctx.result.final_url) == "http"
    ):
        issues.append(
            build_issue(
                IssueType.REDIRECT_HTTP_DOWNGRADE,
                source_url=ctx.source_url,
                target_url=ctx.target_url,
                final_url=ctx.result.final_url,
                status=ctx.result.status,
                reason="https URL redirects down to http",
                element=ctx.element,
                attribute=ctx.attribute,
                link_text=ctx.link_text,
                chain=ctx.result.chain,
            )
        )
    first = redirects[0].status if redirects else None
    if (
        ctx.internal
        and settings.report_temporary_redirects
        and first in TEMPORARY_STATUSES
        and ctx.result.ok
    ):
        issues.append(
            build_issue(
                IssueType.REDIRECT_TEMPORARY_PERMANENT_INTERNAL,
                source_url=ctx.source_url,
                target_url=ctx.target_url,
                final_url=ctx.result.final_url,
                status=first,
                reason=f"HTTP {first} is temporary; a permanent internal link should use 301",
                element=ctx.element,
                attribute=ctx.attribute,
                link_text=ctx.link_text,
                chain=ctx.result.chain,
            )
        )
    return issues


def detect_slow(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """Responses slower than ``detection.slow_ms``."""
    duration = ctx.result.duration_ms
    if ctx.result.error is not None or duration <= settings.slow_ms:
        return []
    return [
        build_issue(
            IssueType.SLOW,
            source_url=ctx.source_url,
            target_url=ctx.target_url,
            final_url=ctx.result.final_url,
            status=ctx.result.status,
            reason=f"responded in {duration} ms (limit {settings.slow_ms} ms)",
            element=ctx.element,
            attribute=ctx.attribute,
            link_text=ctx.link_text,
            chain=ctx.result.chain,
        )
    ]


def detect_mixed_content(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """An http resource referenced from an https page."""
    if not settings.report_mixed_content or _scheme_of(ctx.source_url or "") != "https":
        return []
    if _scheme_of(ctx.target_url) != "http":
        return []
    return [
        build_issue(
            IssueType.MIXED_CONTENT,
            source_url=ctx.source_url,
            target_url=ctx.target_url,
            final_url=ctx.result.final_url or None,
            status=ctx.result.status,
            reason="insecure http resource on an https page",
            element=ctx.element,
            attribute=ctx.attribute,
            link_text=ctx.link_text,
        )
    ]


#: Link-level rules, run for every checked URL (internal page *and* external link).
DETECTORS: tuple[Callable[[LinkContext, DetectionSettings], list[Issue]], ...] = (
    detect_unreachable,
    detect_broken,
    detect_redirect_problems,
    detect_mixed_content,
    detect_slow,
)


def run_link_detectors(
    ctx: LinkContext,
    settings: DetectionSettings,
    *,
    detectors: Iterable[Callable[[LinkContext, DetectionSettings], list[Issue]]] | None = None,
) -> list[Issue]:
    """Apply every link-level detector, de-duplicate by fingerprint, order by severity."""
    out: dict[str, Issue] = {}
    for detector in tuple(DETECTORS if detectors is None else detectors):
        for issue in detector(ctx, settings):
            out.setdefault(issue.fingerprint, issue)
    return sorted(
        out.values(),
        key=lambda issue: (issue.severity != Severity.ERROR, issue.code, issue.target_url),
    )


def detect_missing_fragment(
    *,
    source_url: str | None,
    target_url: str,
    fragment: str,
    anchors: set[str] | None,
    element: str | None = None,
    attribute: str | None = None,
    link_text: str | None = None,
) -> Issue | None:
    """The target page exists but has no element with this ``id``/``name``.

    ``anchors`` is ``None`` when the target page could not be inspected (not crawled,
    blocked by robots, too large to parse): we stay quiet rather than invent an issue.
    """
    if anchors is None or not fragment or fragment in anchors:
        return None
    return build_issue(
        IssueType.MISSING_FRAGMENT,
        source_url=source_url,
        # The fragment is part of the problem, so it belongs in the fingerprint.
        target_url=f"{target_url}#{fragment}",
        final_url=target_url,
        reason=f"no id or name {fragment!r} on the target page",
        element=element,
        attribute=attribute,
        link_text=link_text,
    )


def detect_robots_block(
    *,
    source_url: str | None,
    target_url: str,
    element: str | None = None,
    link_text: str | None = None,
) -> Issue:
    """robots.txt told us not to crawl this internal URL."""
    return build_issue(
        IssueType.BLOCKED_BY_ROBOTS,
        source_url=source_url,
        target_url=target_url,
        reason="robots.txt disallows Linkkeep for this URL, so it was not crawled or checked",
        element=element,
        link_text=link_text,
    )


def severity_at_least(issue: Issue, fail_on: str) -> bool:
    """Does ``issue`` reach the configured ``--fail-on`` threshold?"""
    order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}
    if fail_on == "never":
        return False
    try:
        threshold = order[Severity(fail_on)]
    except ValueError as exc:
        msg = f"unknown --fail-on value: {fail_on}"
        raise ValueError(msg) from exc
    return order[issue.severity] >= threshold


def summarise(issues: Sequence[Issue]) -> dict[str, dict[str, int]]:
    """``{code: {severity: count}}`` - used by the CLI summary line."""
    out: dict[str, dict[str, int]] = {}
    for issue in issues:
        bucket = out.setdefault(issue.code, {})
        key = issue.severity.value
        bucket[key] = bucket.get(key, 0) + 1
    return out


def _scheme_of(url: str) -> str:
    return urlsplit(url).scheme.lower()
