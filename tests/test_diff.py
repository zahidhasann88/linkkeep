"""Diffing two runs: NEW, FIXED, PERSISTING and "broken since"."""

from __future__ import annotations

import datetime as dt

import pytest
from tests.conftest import NOW, issue

from linkkeep.config import LinkkeepError
from linkkeep.diff import (
    PersistingIssue,
    compare_issue_sets,
    describe_diff,
    diff_runs,
    resolve_run_ids,
)
from linkkeep.models import Counts, RunRecord, RunStatus

SITE = "https://site.test"


def stored_run(
    storage: object, started: dt.datetime, items: list, *, status: RunStatus = RunStatus.COMPLETED
) -> int:
    run_id = storage.create_run(SITE, [SITE], {}, started)  # type: ignore[attr-defined]
    counts = Counts(pages=5, errors=sum(1 for i in items if i.severity.value == "error"))
    storage.save_run_data(run_id, [], [], items, counts, status, started + dt.timedelta(seconds=5))  # type: ignore[attr-defined]
    return run_id


# --------------------------------------------------------------- pure comparison
def test_compare_sets_classifies_by_fingerprint() -> None:
    same = issue("broken", target=f"{SITE}/a", status=404)
    same_but_different_status = issue("broken", target=f"{SITE}/a", status=500)
    gone = issue("broken", target=f"{SITE}/b")
    new = issue("unreachable", target=f"{SITE}/c")
    added, removed, persisting = compare_issue_sets([same, gone], [same_but_different_status, new])
    assert [i.target_url for i in added] == [f"{SITE}/c"]
    assert [i.target_url for i in removed] == [f"{SITE}/b"]
    assert [item.issue.fingerprint for item in persisting] == [same.fingerprint]
    # the persisting entry carries the *new* data (status 500) but the old identity
    assert persisting[0].issue.status == 500


def test_compare_sets_sorts_by_severity_then_code() -> None:
    old = [issue("slow", target=f"{SITE}/s")]
    new = [issue("unreachable", target=f"{SITE}/u"), issue("mixed_content", target=f"{SITE}/m")]
    added, _removed, _persisting = compare_issue_sets(old, new)
    assert [i.code for i in added] == ["unreachable", "mixed_content"]


def test_empty_sets_are_fine() -> None:
    assert compare_issue_sets([], []) == ([], [], [])


def test_persisting_issue_age_helpers() -> None:
    item = PersistingIssue(
        issue=issue("broken"), since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    )
    assert item.age_days == 3
    assert item.age_label() == "3 days"
    assert (
        PersistingIssue(issue=issue("broken"), since=dt.datetime.now(dt.timezone.utc)).age_label()
        == "today"
    )
    assert (
        PersistingIssue(
            issue=issue("broken"), since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        ).age_label()
        == "4 weeks"
    )


# ------------------------------------------------------------ storage-backed diff
def test_diff_runs_reports_new_fixed_persisting(storage: object) -> None:
    a_issue = issue("broken", source=f"{SITE}/", target=f"{SITE}/gone", status=410)
    fixed_issue = issue("broken", source=f"{SITE}/", target=f"{SITE}/old", status=404)
    new_issue = issue("unreachable", source=f"{SITE}/", target=f"{SITE}/dead", reason="DNS failure")
    first = stored_run(storage, NOW - dt.timedelta(days=4), [a_issue, fixed_issue])  # type: ignore[arg-type]
    second = stored_run(storage, NOW - dt.timedelta(days=1), [a_issue, new_issue])  # type: ignore[arg-type]

    result = diff_runs(storage, first, second)  # type: ignore[arg-type]
    assert [i.fingerprint for i in result.new] == [new_issue.fingerprint]
    assert [i.fingerprint for i in result.fixed] == [fixed_issue.fingerprint]
    assert [item.issue.fingerprint for item in result.persisting] == [a_issue.fingerprint]
    assert result.site == SITE
    assert result.run_a.id == first
    assert result.run_b.id == second
    assert not result.is_clean
    assert result.new_by_severity() == {"error": 1}
    assert describe_diff(result) == f"{SITE}: 1 new, 1 fixed, 1 persisting"


def test_broken_since_uses_the_earliest_run(storage: object) -> None:
    a_issue = issue("broken", target=f"{SITE}/gone")
    first = stored_run(storage, NOW - dt.timedelta(days=30), [a_issue])  # type: ignore[arg-type]
    second = stored_run(storage, NOW - dt.timedelta(days=10), [a_issue])  # type: ignore[arg-type]
    third = stored_run(storage, NOW - dt.timedelta(days=1), [a_issue])  # type: ignore[arg-type]

    result = diff_runs(storage, second, third)  # type: ignore[arg-type]
    assert result.persisting[0].since == NOW - dt.timedelta(days=30)
    assert result.persisting[0].runs_seen == 3

    # comparing the two oldest runs reports the first run as the origin
    early = diff_runs(storage, first, second)  # type: ignore[arg-type]
    assert early.persisting[0].since == NOW - dt.timedelta(days=30)


def test_identical_runs_produce_an_empty_diff(storage: object) -> None:
    only = issue("slow", target=f"{SITE}/t")
    first = stored_run(storage, NOW - dt.timedelta(days=1), [only])  # type: ignore[arg-type]
    second = stored_run(storage, NOW, [only])  # type: ignore[arg-type]
    result = diff_runs(storage, first, second)  # type: ignore[arg-type]
    assert result.new == []
    assert result.fixed == []
    assert result.is_clean
    assert len(result.persisting) == 1


def test_notes_about_unfinished_runs(storage: object) -> None:
    first = stored_run(storage, NOW - dt.timedelta(days=1), [], status=RunStatus.INTERRUPTED)  # type: ignore[arg-type]
    second = stored_run(storage, NOW, [issue("broken", target=f"{SITE}/x")])  # type: ignore[arg-type]
    result = diff_runs(storage, first, second)  # type: ignore[arg-type]
    assert result.new
    assert any("baseline" in note for note in result.notes)


def test_unknown_runs_and_wrong_order_are_friendly_errors(storage: object) -> None:
    first = stored_run(storage, NOW, [])  # type: ignore[arg-type]
    second = stored_run(storage, NOW + dt.timedelta(days=1), [])  # type: ignore[arg-type]
    with pytest.raises(LinkkeepError, match="no run with id 999"):
        diff_runs(storage, 999, second)  # type: ignore[arg-type]
    with pytest.raises(LinkkeepError, match="newer than"):
        diff_runs(storage, second, first)  # type: ignore[arg-type]


def test_other_site_is_refused(storage: object) -> None:
    mine = stored_run(storage, NOW, [])  # type: ignore[arg-type]
    theirs = storage.create_run("https://other.test", [], {}, NOW)  # type: ignore[attr-defined]
    with pytest.raises(LinkkeepError, match="same site"):
        diff_runs(storage, mine, theirs)  # type: ignore[arg-type]


# ------------------------------------------------------------------- run picking
def test_resolve_defaults_to_the_two_most_recent(storage: object) -> None:
    ids = [stored_run(storage, NOW + dt.timedelta(days=index), []) for index in range(3)]  # type: ignore[arg-type]
    assert resolve_run_ids(storage, None, None) == (ids[1], ids[2])  # type: ignore[arg-type]
    assert resolve_run_ids(storage, None, ids[2]) == (ids[1], ids[2])  # type: ignore[arg-type]
    assert resolve_run_ids(storage, ids[0], ids[2]) == (ids[0], ids[2])  # type: ignore[arg-type]
    assert resolve_run_ids(storage, ids[0], None) == (ids[0], ids[2])  # type: ignore[arg-type]


def test_resolve_needs_two_runs(storage: object) -> None:
    only = stored_run(storage, NOW, [])  # type: ignore[arg-type]
    with pytest.raises(LinkkeepError, match="fewer than two runs"):
        resolve_run_ids(storage, None, None)  # type: ignore[arg-type]
    with pytest.raises(LinkkeepError, match="no older run"):
        resolve_run_ids(storage, None, only)  # type: ignore[arg-type]


def test_resolve_can_filter_by_site(storage: object) -> None:
    stored_run(storage, NOW, [])  # type: ignore[arg-type]
    storage.create_run("https://other.test", [], {}, NOW + dt.timedelta(days=1))  # type: ignore[attr-defined]
    with pytest.raises(LinkkeepError, match="fewer than two runs"):
        resolve_run_ids(storage, None, None, site=SITE)  # type: ignore[arg-type]


def test_run_record_model_defaults() -> None:
    run = RunRecord(id=1, site=SITE, started_at=NOW)
    assert run.status is RunStatus.RUNNING
    assert run.counts.total_issues == 0
    assert run.finished_at is None
