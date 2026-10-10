"""Comparing two stored runs: what is NEW, what is FIXED, what is PERSISTING.

Identity is the issue fingerprint (``type + source + target``), so a link that was a
404 last week and is a 500 today is still *the same issue* - it persists, and we can
say since when.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from linkkeep.config import LinkkeepError
from linkkeep.models import Issue, RunRecord, to_utc

SortKey = Callable[[Issue], tuple[Any, ...]]


@dataclass
class PersistingIssue:
    """An issue that was already present in the older run."""

    issue: Issue
    since: dt.datetime
    runs_seen: int = 1

    @property
    def age_days(self) -> int:
        return max(0, (dt.datetime.now(dt.timezone.utc) - to_utc(self.since)).days)

    def age_label(self, *, now: dt.datetime | None = None) -> str:
        """Human age of the finding.  Pass ``now`` to keep rendered output deterministic."""
        days = self.age_days if now is None else max(0, (to_utc(now) - to_utc(self.since)).days)
        if days == 0:
            return "today"
        if days == 1:
            return "1 day"
        if days < 14:
            return f"{days} days"
        return f"{days // 7} weeks"


@dataclass
class DiffResult:
    """The result of comparing run A (older) with run B (newer)."""

    site: str
    run_a: RunRecord
    run_b: RunRecord
    new: list[Issue] = field(default_factory=list)
    fixed: list[Issue] = field(default_factory=list)
    persisting: list[PersistingIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_new(self) -> bool:
        return bool(self.new)

    def new_by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.new:
            key = issue.severity.value
            out[key] = out.get(key, 0) + 1
        return out

    @property
    def is_clean(self) -> bool:
        return not self.new


def compare_issue_sets(
    old: Iterable[Issue],
    new: Iterable[Issue],
    *,
    first_seen: dict[str, dt.datetime] | None = None,
    runs_seen: dict[str, int] | None = None,
) -> tuple[list[Issue], list[Issue], list[PersistingIssue]]:
    """Pure diff of two issue collections (used by :func:`diff_runs` and by tests)."""
    old_by_fp = {issue.fingerprint: issue for issue in old}
    new_by_fp = {issue.fingerprint: issue for issue in new}
    added = [issue for fp, issue in new_by_fp.items() if fp not in old_by_fp]
    removed = [issue for fp, issue in old_by_fp.items() if fp not in new_by_fp]
    seen = first_seen or {}
    counts = runs_seen or {}
    persisting: list[PersistingIssue] = []
    for fp, issue in new_by_fp.items():
        if fp in old_by_fp:
            persisting.append(
                PersistingIssue(
                    issue=issue,
                    since=seen.get(fp, issue.found_at),
                    runs_seen=counts.get(fp, 2),
                )
            )
    added.sort(key=lambda issue: issue.sort_key())
    removed.sort(key=lambda issue: issue.sort_key())
    persisting.sort(key=lambda item: item.issue.sort_key())
    return added, removed, persisting


def diff_runs(storage: Any, run_a_id: int, run_b_id: int) -> DiffResult:
    """Compare two stored runs of the same site (``run_a_id`` must be the older one)."""
    run_a = storage.get_run(run_a_id)
    run_b = storage.get_run(run_b_id)
    if run_a is None:
        raise LinkkeepError(f"no run with id {run_a_id} in {storage.path}")
    if run_b is None:
        raise LinkkeepError(f"no run with id {run_b_id} in {storage.path}")
    if run_a.site != run_b.site:
        raise LinkkeepError(
            f"run {run_a_id} is for {run_a.site} but run {run_b_id} is for {run_b.site}; "
            "diff only compares crawls of the same site"
        )
    if to_utc(run_a.started_at) > to_utc(run_b.started_at):
        raise LinkkeepError(
            f"run {run_a_id} is newer than run {run_b_id}; pass the older run first "
            "(see `linkkeep runs`)"
        )
    issues_a = storage.issues_for_run(run_a_id)
    issues_b = storage.issues_for_run(run_b_id)
    fingerprints = [issue.fingerprint for issue in issues_b]
    seen = storage.first_seen(run_b.site, fingerprints, before_run=run_b_id)
    counts = _runs_seen(storage, run_b.site, fingerprints, up_to=run_b_id)
    for fp in seen:
        counts.setdefault(fp, 1)
    new, fixed, persisting = compare_issue_sets(
        issues_a, issues_b, first_seen=seen, runs_seen=counts
    )
    notes: list[str] = []
    if run_a.status.value != "completed":
        notes.append(f"baseline run {run_a_id} has status {run_a.status.value}")
    if run_b.status.value != "completed":
        notes.append(f"current run {run_b_id} has status {run_b.status.value}")
    return DiffResult(
        site=run_b.site,
        run_a=run_a,
        run_b=run_b,
        new=new,
        fixed=fixed,
        persisting=persisting,
        notes=notes,
    )


def _runs_seen(
    storage: Any, site: str, fingerprints: Sequence[str], *, up_to: int
) -> dict[str, int]:
    wanted = [fp for fp in fingerprints if fp]
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    sql = (
        "SELECT i.fingerprint AS fp, COUNT(DISTINCT i.run_id) AS runs"
        " FROM issues i JOIN runs r ON r.id = i.run_id"
        f" WHERE r.site = ? AND i.fingerprint IN ({placeholders}) AND i.run_id <= ?"
        " GROUP BY i.fingerprint"
    )
    rows = storage.connection.execute(sql, [site, *wanted, up_to]).fetchall()
    return {str(row["fp"]): int(row["runs"]) for row in rows}


def resolve_run_ids(
    storage: Any, requested_a: int | None, requested_b: int | None, *, site: str | None = None
) -> tuple[int, int]:
    """Pick the two most recent runs when no ids were given."""
    runs: list[RunRecord] = storage.list_runs(site=site, limit=200)
    if requested_a is not None and requested_b is not None:
        return requested_a, requested_b
    if requested_b is None:
        if len(runs) < 2:
            raise LinkkeepError(
                "this database has fewer than two runs for that site, so there is nothing to "
                "compare; run `linkkeep crawl` again first"
            )
        newest, previous = runs[0], runs[1]
        if requested_a is not None:
            return requested_a, newest.id
        return previous.id, newest.id
    newest_id = requested_b
    if requested_a is not None:
        return requested_a, newest_id
    older = [run for run in runs if run.id < newest_id]
    if not older:
        raise LinkkeepError(f"no older run found to compare with run {newest_id}")
    return older[0].id, newest_id


def describe_diff(diff: DiffResult) -> str:
    """One-line summary used by ``linkkeep watch`` and the CI example."""
    parts = [
        f"{len(diff.new)} new",
        f"{len(diff.fixed)} fixed",
        f"{len(diff.persisting)} persisting",
    ]
    return f"{diff.site}: " + ", ".join(parts)
