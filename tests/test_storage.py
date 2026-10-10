"""SQLite storage: schema, migrations, round trips, first_seen, pruning."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from tests.conftest import NOW, issue

from linkkeep.models import (
    Counts,
    LinkKind,
    LinkRecord,
    PageRecord,
    RedirectHop,
    RunRecord,
    RunStatus,
    utcnow,
)
from linkkeep.storage import MIGRATIONS, SCHEMA_VERSION, Storage, fingerprint_for, severity_order

SITE = "https://site.test"


def pages(count: int = 2) -> list[PageRecord]:
    return [
        PageRecord(
            run_id=0,
            url=f"{SITE}/page-{index}",
            depth=index,
            status=200,
            final_url=f"{SITE}/page-{index}",
            duration_ms=10 + index,
            content_type="text/html",
            size_bytes=200,
            links_found=3,
            links_checked=2,
        )
        for index in range(count)
    ]


def links() -> list[LinkRecord]:
    return [
        LinkRecord(
            run_id=0,
            source_url=f"{SITE}/page-0",
            target_url=f"{SITE}/missing",
            kind=LinkKind.INTERNAL,
            element="a",
            attribute="href",
            link_text="Missing",
            final_url=f"{SITE}/missing",
            status=404,
            duration_ms=12,
            scheme="https",
            checked=True,
            redirect_chain=[RedirectHop(url=f"{SITE}/missing", status=404)],
        ),
        LinkRecord(
            run_id=0,
            source_url=f"{SITE}/page-1",
            target_url="https://other.test/x",
            kind=LinkKind.EXTERNAL,
            element="a",
            status=200,
            scheme="https",
            checked=True,
        ),
    ]


def issues() -> list:
    return [
        issue("broken", source=f"{SITE}/page-0", target=f"{SITE}/missing", status=404),
        issue(
            "slow",
            source=f"{SITE}/page-1",
            target="https://other.test/x",
            reason="responded in 9000 ms",
        ),
    ]


# ------------------------------------------------------------------------ schema
def test_fresh_database_has_every_table(storage: Storage) -> None:
    names = {
        row["name"]
        for row in storage.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"runs", "pages", "links", "issues"} <= names
    assert storage.schema_version == SCHEMA_VERSION
    assert storage.has_runs_table()


def test_migrations_are_numbered_and_contiguous() -> None:
    assert sorted(MIGRATIONS) == list(range(1, SCHEMA_VERSION + 1))
    assert max(MIGRATIONS) == SCHEMA_VERSION


def test_upgrade_from_older_schema(tmp_path: Path) -> None:
    """A v1 database gains the v2 columns and keeps its rows."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    for statement in MIGRATIONS[1]:
        conn.executescript(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO runs (site, started_at, status, seeds, config, pages) VALUES (?,?,?,?,?,?)",
        (SITE, NOW.isoformat(), "completed", "[]", "{}", 7),
    )
    conn.commit()
    conn.close()

    storage = Storage(path)
    assert storage.schema_version == SCHEMA_VERSION
    run = storage.list_runs()[0]
    assert isinstance(run, RunRecord)
    assert run.counts.pages == 7
    assert run.notes is None  # the column added by migration v2
    storage.close()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    Storage(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer Linkkeep"):
        Storage(path)


# ------------------------------------------------------------------- run writes
def test_create_and_finish_a_run(storage: Storage) -> None:
    run_id = storage.create_run(SITE, [SITE], {"crawl": {"max_pages": 10}}, NOW)
    assert run_id > 0
    running = storage.get_run(run_id)
    assert running is not None
    assert running.status is RunStatus.RUNNING
    assert running.seeds == [SITE]
    assert running.config == {"crawl": {"max_pages": 10}}

    storage.save_run_data(
        run_id,
        pages(3),
        links(),
        issues(),
        Counts(
            pages=3,
            links=2,
            checked=2,
            errors=1,
            warnings=0,
            infos=1,
            by_type={"broken": 1, "slow": 1},
        ),
        RunStatus.COMPLETED,
        NOW + dt.timedelta(seconds=12),
        ["sitemap added 1 URL(s)"],
        duration_ms=12000,
    )
    done = storage.get_run(run_id)
    assert done is not None
    assert done.status is RunStatus.COMPLETED
    assert done.counts.pages == 3
    assert done.counts.errors == 1
    assert done.counts.by_type == {"broken": 1, "slow": 1}
    assert done.notes == "sitemap added 1 URL(s)"

    stored_pages = storage.pages_for_run(run_id)
    assert [p.url for p in stored_pages] == [f"{SITE}/page-{i}" for i in range(3)]
    assert stored_pages[0].links_checked == 2

    stored_links = storage.links_for_run(run_id)
    assert len(stored_links) == 2
    external = next(link for link in stored_links if link.kind is LinkKind.EXTERNAL)
    assert external.status == 200
    assert external.checked is True
    internal = next(link for link in stored_links if link.kind is LinkKind.INTERNAL)
    assert internal.link_text == "Missing"
    assert internal.redirect_chain[0].status == 404

    stored_issues = storage.issues_for_run(run_id)
    assert [i.code for i in stored_issues] == ["broken", "slow"]  # ordered by severity then code
    assert stored_issues[0].status == 404
    assert stored_issues[0].found_at == NOW


def test_saving_twice_replaces_instead_of_duplicating(storage: Storage) -> None:
    run_id = storage.create_run(SITE, [SITE], {}, NOW)
    storage.save_run_data(
        run_id, pages(2), links(), issues(), Counts(pages=2), RunStatus.RUNNING, None
    )
    storage.save_run_data(
        run_id, pages(5), links(), issues(), Counts(pages=5), RunStatus.RUNNING, None
    )
    assert len(storage.pages_for_run(run_id)) == 5
    assert len(storage.links_for_run(run_id)) == 2
    assert len(storage.issues_for_run(run_id)) == 2
    assert storage.counts(run_id).pages == 5


def test_duplicate_fingerprints_within_a_run_are_ignored(storage: Storage) -> None:
    run_id = storage.create_run(SITE, [SITE], {}, NOW)
    doubled = [issues()[0], issues()[0]]
    storage.save_run_data(run_id, [], [], doubled, Counts(), RunStatus.COMPLETED, None)
    assert len(storage.issues_for_run(run_id)) == 1


def test_update_run_status_keeps_existing_finish_time(storage: Storage) -> None:
    run_id = storage.create_run(SITE, [SITE], {}, NOW)
    storage.save_run_data(
        run_id, [], [], [], Counts(), RunStatus.COMPLETED, NOW + dt.timedelta(seconds=1)
    )
    storage.update_run_status(run_id, RunStatus.INTERRUPTED, ["forced"])
    run = storage.get_run(run_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED
    assert run.finished_at == NOW + dt.timedelta(seconds=1)
    assert run.notes == "forced"


def test_missing_run_raises_on_report_rows(storage: Storage) -> None:
    with pytest.raises(KeyError, match="no run with id"):
        storage.report_rows(999)
    assert storage.get_run(999) is None
    assert storage.counts(999).pages == 0


def test_persistence_across_reopen(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    run_id = storage.create_run(SITE, [SITE], {}, NOW)
    storage.save_run_data(
        run_id, pages(1), links(), issues(), Counts(pages=1, errors=1), RunStatus.COMPLETED, NOW
    )
    storage.close()
    reopened = Storage(tmp_path / "db.sqlite")
    assert len(reopened.issues_for_run(run_id)) == 2
    assert reopened.sites() == [SITE]
    reopened.close()


# --------------------------------------------------------------------- listing
def test_list_runs_is_newest_first_and_filtered(storage: Storage) -> None:
    first = storage.create_run(SITE, [SITE], {}, NOW)
    second = storage.create_run(SITE, [SITE], {}, NOW + dt.timedelta(hours=1))
    other = storage.create_run("https://other.test", [], {}, NOW + dt.timedelta(hours=2))
    assert [run.id for run in storage.list_runs()] == [other, second, first]
    assert [run.id for run in storage.list_runs(site=SITE)] == [second, first]
    assert [run.id for run in storage.list_runs(limit=1)] == [other]
    assert storage.latest_run(SITE).id == second  # type: ignore[union-attr]
    assert storage.run_ids_for_site("https://nothing.test") == []
    assert storage.latest_run("https://nothing.test") is None


# ------------------------------------------------------------------ first_seen
def test_first_seen_tracks_the_oldest_occurrence(storage: Storage) -> None:
    broken = issue("broken", source=f"{SITE}/a", target=f"{SITE}/b", status=404)
    first = storage.create_run(SITE, [SITE], {}, NOW - dt.timedelta(days=9))
    storage.save_run_data(first, [], [], [broken], Counts(), RunStatus.COMPLETED, NOW)
    second = storage.create_run(SITE, [SITE], {}, NOW - dt.timedelta(days=2))
    storage.save_run_data(second, [], [], [broken], Counts(), RunStatus.COMPLETED, NOW)
    third = storage.create_run(SITE, [SITE], {}, NOW)
    storage.save_run_data(third, [], [], [broken], Counts(), RunStatus.COMPLETED, NOW)

    seen = storage.first_seen(SITE, [broken.fingerprint])
    assert seen[broken.fingerprint] == NOW - dt.timedelta(days=9)
    # excluding the newest run still reports the same first sighting
    assert (
        storage.first_seen(SITE, [broken.fingerprint], before_run=third)[broken.fingerprint]
        == seen[broken.fingerprint]
    )
    assert storage.first_seen(SITE, []) == {}
    assert storage.first_seen("https://no.test", [broken.fingerprint]) == {}


def test_runs_seen_counts(storage: Storage) -> None:
    from linkkeep.diff import _runs_seen

    broken = issue("broken", source=f"{SITE}/a", target=f"{SITE}/b")
    for offset in (1, 2, 3):
        run_id = storage.create_run(SITE, [SITE], {}, NOW + dt.timedelta(days=offset))
        storage.save_run_data(run_id, [], [], [broken], Counts(), RunStatus.COMPLETED, NOW)
    counts = _runs_seen(storage, SITE, [broken.fingerprint], up_to=999)
    assert counts[broken.fingerprint] == 3
    assert _runs_seen(storage, SITE, [], up_to=999) == {}


# ----------------------------------------------------------------------- prune
def test_prune_removes_old_runs_but_keeps_the_newest(storage: Storage) -> None:
    old_ids = [
        storage.create_run(SITE, [SITE], {}, NOW - dt.timedelta(days=200 - index))
        for index in range(3)
    ]
    for run_id in old_ids:
        storage.save_run_data(
            run_id, pages(2), links(), issues(), Counts(pages=2), RunStatus.COMPLETED, NOW
        )
    kept = storage.create_run(SITE, [SITE], {}, NOW)
    storage.save_run_data(kept, pages(1), [], [], Counts(pages=1), RunStatus.COMPLETED, NOW)

    deleted = storage.prune(older_than=NOW - dt.timedelta(days=90))
    assert sorted(deleted) == sorted(old_ids)
    assert storage.get_run(kept) is not None
    assert storage.pages_for_run(kept)
    for run_id in old_ids:
        assert storage.get_run(run_id) is None
        assert storage.pages_for_run(run_id) == []
    # nothing left to prune
    assert storage.prune(older_than=NOW - dt.timedelta(days=90)) == []


def test_prune_respects_keep_last_per_site(storage: Storage) -> None:
    old = NOW - dt.timedelta(days=400)
    a1 = storage.create_run("https://a.test", [], {}, old)
    a2 = storage.create_run("https://a.test", [], {}, old + dt.timedelta(days=1))
    b1 = storage.create_run("https://b.test", [], {}, old)
    # keep_last=1: the newest run of each site survives, even though all are old
    assert sorted(storage.prune(older_than=NOW)) == [a1]
    assert storage.get_run(a2) is not None
    assert storage.get_run(b1) is not None
    # keep_last=0 honours the caller and removes everything older than the cutoff
    assert sorted(storage.prune(older_than=NOW, keep_last=0)) == [a2, b1]


def test_vacuum_and_json_fallbacks(storage: Storage) -> None:
    storage.vacuum()
    run_id = storage.create_run(SITE, [SITE], {}, NOW)
    storage.connection.execute(
        "UPDATE runs SET by_type = 'not json', seeds = '' WHERE id = ?", (run_id,)
    )
    run = storage.get_run(run_id)
    assert run is not None
    assert run.counts.by_type == {}
    assert run.seeds == []


def test_helpers() -> None:
    from linkkeep.models import IssueType

    assert fingerprint_for(IssueType.BROKEN, "a", "b") == fingerprint_for(
        IssueType.BROKEN, "a", "b"
    )
    assert severity_order(issue("broken")) < severity_order(issue("slow"))


def test_created_at_defaults_and_serialisation(storage: Storage) -> None:
    run_id = storage.create_run(SITE, [SITE], {"json": [1, 2]}, utcnow())
    item = issue("unreachable", reason="DNS failure: nope")
    storage.save_run_data(run_id, [], [], [item], Counts(errors=1), RunStatus.COMPLETED, utcnow())
    loaded = storage.issues_for_run(run_id)[0]
    assert loaded.reason == "DNS failure: nope"
    assert loaded.found_at.tzinfo is not None
    assert json.dumps(storage.get_run(run_id).config)  # type: ignore[union-attr]
