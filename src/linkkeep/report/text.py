"""Plain-text report: grouped by issue type, then by source page."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from linkkeep.models import Issue, RunStatus
from linkkeep.report import (
    ReportData,
    chain_summary,
    describe_issue,
    shorten,
    since_text,
    stamp,
    summary_line,
    where_seen,
)

RULE = "=" * 78
THIN = "-" * 78


def render(data: ReportData) -> str:
    lines: list[str] = [header(data), "", counts_block(data)]
    if data.issues:
        lines += ["", "ISSUES", THIN]
        for issue_type, issues in data.issues_by_type.items():
            lines += _type_block(issue_type, issues, data)
    else:
        lines += ["", "No issues found.", ""]
    if data.diff is not None:
        lines += ["", diff_block(data)]
    lines.append("")
    return "\n".join(lines)


def header(data: ReportData) -> str:
    run = data.run
    lines = [
        RULE,
        f"Linkkeep {data.tool_version} - run #{run.id} - {run.site}",
        RULE,
        f"status     {run.status.value}"
        + (
            f"   finished {stamp(run.finished_at)}"
            if run.finished_at
            else f"   started {stamp(run.started_at)}"
        ),
    ]
    notes = getattr(run, "notes", None)
    if notes:
        lines.append(f"notes      {shorten(notes, 60)}")
    return "\n".join(lines)


def counts_block(data: ReportData) -> str:
    counts = data.run.counts
    lines = [
        summary_line(data.counts_by_severity),
        (
            f"pages      {counts.pages} crawled"
            + (f" ({counts.robots_blocked} blocked by robots)" if counts.robots_blocked else "")
        ),
        f"links      {counts.links} found, {counts.checked} checked"
        + (f", {counts.skipped_scheme} non-http skipped" if counts.skipped_scheme else "")
        + (f", {counts.excluded} excluded" if counts.excluded else ""),
    ]
    if counts.by_type:
        breakdown = ", ".join(f"{code}={count}" for code, count in sorted(counts.by_type.items()))
        lines.append(f"by type    {breakdown}")
    lines.append(f"duration   {data.duration_seconds:.1f}s")
    return "\n".join(lines)


def _type_block(issue_type: Any, issues: list[Issue], data: ReportData) -> list[str]:
    from linkkeep.models import DESCRIPTION_BY_TYPE, SEVERITY_BY_TYPE

    severity = SEVERITY_BY_TYPE[issue_type]
    out = [
        "",
        f"[{severity.value.upper()}] {issue_type.value} - {len(issues)}"
        f"  ({DESCRIPTION_BY_TYPE[issue_type]})",
    ]
    for source_url, group in data.issues_by_source(issues).items():
        out.append(f"  from: {source_url or '(seed URL, no source page)'}")
        for issue in group:
            out.append(f"    {shorten(issue.target_url, 66)}")
            detail = f"      {describe_issue(issue)}"
            extras = [detail]
            seen = where_seen(issue)
            if seen:
                extras.append(seen)
            out.append(" ".join(extras))
            chain = chain_summary(issue)
            if chain:
                out.append(f"      {chain}")
    return out


def diff_block(data: ReportData) -> str:
    """``NEW`` / ``FIXED`` / ``PERSISTING`` section shared with ``linkkeep diff``."""
    diff = data.diff
    if diff is None:
        return ""
    lines = [
        RULE,
        f"DIFF  run #{diff.run_b.id} vs run #{diff.run_a.id}  ({diff.site})",
        f"      older crawl {stamp(diff.run_a.started_at)}, newer crawl {stamp(diff.run_b.started_at)}",
        RULE,
    ]
    lines += _section("NEW", "problems that were not there last time", diff.new, indent="")
    lines += _section("FIXED", "problems that are gone now", diff.fixed, indent="")
    persisting = [item.issue for item in diff.persisting]
    lines += _section(
        "PERSISTING",
        "still broken",
        persisting,
        indent="",
        extra=since_labels(diff.persisting, now=data.generated_at),
    )
    for note in diff.notes:
        lines.append(f"note: {note}")
    if not (diff.new or diff.fixed or diff.persisting):
        lines.append("Both runs report the same set of issues.")
    return "\n".join(lines)


def since_labels(items: Iterable[Any], *, now: dt.datetime | None = None) -> dict[str, str]:
    """``fingerprint -> 'broken since ...'`` annotations for the PERSISTING block."""
    return {item.issue.fingerprint: since_text(item.since, now=now) for item in items}


def _section(
    title: str,
    blurb: str,
    issues: list[Issue],
    *,
    indent: str = "  ",
    extra: dict[str, str] | None = None,
) -> list[str]:
    out = [f"{indent}{title} ({len(issues)}) - {blurb}"]
    if not issues:
        out[-1] += ": none"
        return out
    for issue in issues:
        line = f"{indent}  [{issue.severity.value}] {issue.code}: {shorten(issue.target_url, 58)}"
        reason = describe_issue(issue)
        if reason:
            line += f" ({reason})"
        out.append(line)
        if issue.source_url:
            out.append(f"{indent}      from {issue.source_url}")
        note = (extra or {}).get(issue.fingerprint)
        if note:
            out.append(f"{indent}      {note}")
    return out


def status_note(status: RunStatus) -> str:
    return {
        RunStatus.RUNNING: "still running",
        RunStatus.COMPLETED: "finished",
        RunStatus.INTERRUPTED: "stopped early (Ctrl+C)",
        RunStatus.FAILED: "could not run",
    }.get(status, status.value)
