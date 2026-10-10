"""Markdown report - sized for a GitHub issue or PR comment."""

from __future__ import annotations

from collections.abc import Sequence

from linkkeep.models import Issue, IssueType, RunStatus, Severity
from linkkeep.report import ReportData, describe_issue, shorten, since_text, stamp

BADGE = {
    Severity.ERROR: ":x:",
    Severity.WARNING: ":warning:",
    Severity.INFO: ":information_source:",
}

EMOJI_BY_TYPE: dict[IssueType, str] = {
    IssueType.BROKEN: ":link:",
    IssueType.UNREACHABLE: ":satellite:",
    IssueType.REDIRECT_CHAIN: ":arrows_counterclockwise:",
    IssueType.REDIRECT_LOOP: ":cyclone:",
    IssueType.REDIRECT_TO_ERROR: ":arrow_heading_down:",
    IssueType.REDIRECT_HTTP_DOWNGRADE: ":lock:",
    IssueType.REDIRECT_TEMPORARY_PERMANENT_INTERNAL: ":hourglass:",
    IssueType.MIXED_CONTENT: ":unlock:",
    IssueType.SLOW: ":turtle:",
    IssueType.MISSING_FRAGMENT: ":target:",
    IssueType.BLOCKED_BY_ROBOTS: ":robot:",
}


def render(data: ReportData) -> str:
    lines = [f"## Linkkeep: {data.run.site}", ""]
    lines += _summary(data)
    if data.issues:
        lines += ["", "### Issues", "", _table(data.issues)]
        lines += ["", "### Details", ""]
        for issue_type, issues in data.issues_by_type.items():
            lines += _type_details(issue_type, issues, data)
    else:
        lines += ["", "No issues found.", ""]
    if data.diff is not None:
        lines += ["", _diff_section(data)]
    return "\n".join(lines).rstrip() + "\n"


def _summary(data: ReportData) -> list[str]:
    run = data.run
    counts = run.counts
    emoji = {
        RunStatus.COMPLETED: ":white_check_mark:",
        RunStatus.INTERRUPTED: ":warning:",
        RunStatus.FAILED: ":no_entry:",
        RunStatus.RUNNING: ":hourglass_flowing_sand:",
    }[run.status]
    lines = [
        f"{emoji} **Run #{run.id}** `{run.status.value}` - {stamp(run.started_at)} UTC, "
        f"{data.duration_seconds:.1f}s",
        "",
        "| severity | count | pages | links checked | robots-blocked |",
        "| --- | --- | --- | --- | --- |",
        (
            f"| :x: errors | {data.counts_by_severity[Severity.ERROR]} | {counts.pages} | "
            f"{counts.checked} | {counts.robots_blocked} |"
        ),
        (f"| :warning: warnings | {data.counts_by_severity[Severity.WARNING]} | | | |"),
        (f"| :information_source: info | {data.counts_by_severity[Severity.INFO]} | | | |"),
    ]
    if run.notes:
        lines += ["", "> [!NOTE]", f"> {run.notes}"]
    return lines


def _table(issues: Sequence[Issue]) -> str:
    rows = [
        "| | issue | target | status | source page |",
        "| --- | --- | --- | --- | --- |",
    ]
    for issue in issues:
        rows.append(
            f"| {BADGE[issue.severity]} | `{issue.code}` | {md_link(issue.target_url)} | "
            f"{issue.status if issue.status is not None else '-'} | {md_link(issue.source_url)} |"
        )
    return "\n".join(rows)


def _type_details(issue_type: IssueType, issues: list[Issue], data: ReportData) -> list[str]:
    from linkkeep.models import DESCRIPTION_BY_TYPE

    lines = [
        f"<details><summary>{EMOJI_BY_TYPE.get(issue_type, ':bug:')} <code>{issue_type.value}</code> "
        f"({len(issues)}) - {DESCRIPTION_BY_TYPE[issue_type]}</summary>",
        "",
    ]
    for source_url, group in data.issues_by_source(issues).items():
        lines.append(f"- **from** {md_link(source_url)}")
        for issue in group:
            text = f"  - {md_link(issue.target_url)} - {describe_issue(issue)}"
            if issue.link_text:
                text += f", label {shorten(issue.link_text, 40)!r}".replace("'", "`")
            lines.append(text)
            if issue.reason and issue.status is None:
                lines.append(f"    - {issue.reason}")
    lines += ["", "</details>", ""]
    return lines


def _diff_section(data: ReportData) -> str:
    diff = data.diff
    assert diff is not None
    lines = [
        "### Changes since the previous crawl",
        "",
        f"comparing run #{diff.run_a.id} ({stamp(diff.run_a.started_at)}) with run #{diff.run_b.id} "
        f"({stamp(diff.run_b.started_at)})",
        "",
    ]
    if not diff.new:
        lines.append(":white_check_mark: **No new problems.**")
    else:
        lines += [f":rotating_light: **{len(diff.new)} new problem(s):**", ""]
        lines += [_table(diff.new)]
    lines.append("")
    if diff.fixed:
        lines += [f":checkered_flag: **{len(diff.fixed)} fixed:**", "", _table(diff.fixed), ""]
    if diff.persisting:
        lines += [f":hourglass: **{len(diff.persisting)} still broken:**", ""]
        for item in diff.persisting:
            lines.append(
                f"- `{item.issue.code}` {md_link(item.issue.target_url)} - "
                f"{since_text(item.since, now=data.generated_at)}, seen in {item.runs_seen} runs"
            )
    for note in diff.notes:
        lines.append(f"- note: {note}")
    return "\n".join(lines)


def md_link(url: str | None) -> str:
    if not url:
        return "-"
    return f"[`{shorten(url, 70)}`]({url})"
