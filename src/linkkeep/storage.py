"""SQLite storage: schema, migrations, run/page/link/issue rows.

Storage is synchronous on purpose (stdlib ``sqlite3``, no ORM).  The async crawler
calls it through ``asyncio.to_thread`` and every write is one transaction, so a lost
process can only lose the last flush.

Fingerprints
------------
``issues.fingerprint`` is ``sha1(type + source + target)`` (see
:func:`linkkeep.models.make_fingerprint`).  It is what lets ``linkkeep diff`` say
"this problem is the same one as last week" even when the status code or final URL
changed, and ``first_seen`` gives us "broken since".
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from linkkeep.config import database_path, ensure_parent
from linkkeep.models import (
    Counts,
    Issue,
    IssueType,
    LinkKind,
    LinkRecord,
    PageRecord,
    RedirectHop,
    RunRecord,
    RunStatus,
    Severity,
    make_fingerprint,
    to_utc,
)

#: Bump when the schema changes and add a matching entry to MIGRATIONS.
SCHEMA_VERSION = 2

#: ``target version -> SQL statements`` applied in order, one transaction each.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            site         TEXT    NOT NULL,
            started_at   TEXT    NOT NULL,
            finished_at  TEXT,
            status       TEXT    NOT NULL DEFAULT 'running',
            seeds        TEXT    NOT NULL DEFAULT '[]',
            pages        INTEGER NOT NULL DEFAULT 0,
            links        INTEGER NOT NULL DEFAULT 0,
            checked      INTEGER NOT NULL DEFAULT 0,
            errors       INTEGER NOT NULL DEFAULT 0,
            warnings     INTEGER NOT NULL DEFAULT 0,
            infos        INTEGER NOT NULL DEFAULT 0,
            skipped      INTEGER NOT NULL DEFAULT 0,
            excluded     INTEGER NOT NULL DEFAULT 0,
            robots_blocked INTEGER NOT NULL DEFAULT 0,
            by_type      TEXT    NOT NULL DEFAULT '{}',
            config       TEXT    NOT NULL DEFAULT '{}',
            duration_ms  INTEGER
        );
        CREATE INDEX idx_runs_site ON runs (site, started_at DESC);
        """,
        """
        CREATE TABLE pages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            url          TEXT    NOT NULL,
            depth        INTEGER NOT NULL DEFAULT 0,
            status       INTEGER,
            final_url    TEXT,
            duration_ms  INTEGER,
            content_type TEXT,
            size_bytes   INTEGER,
            error        TEXT,
            crawlable    INTEGER NOT NULL DEFAULT 1,
            links_found  INTEGER NOT NULL DEFAULT 0,
            links_checked INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX idx_pages_run_url ON pages (run_id, url);
        """,
        """
        CREATE TABLE links (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            source_url   TEXT    NOT NULL,
            target_url   TEXT    NOT NULL,
            kind         TEXT    NOT NULL DEFAULT 'internal',
            element      TEXT,
            attribute    TEXT,
            link_text    TEXT,
            final_url    TEXT,
            status       INTEGER,
            duration_ms  INTEGER,
            error        TEXT,
            scheme       TEXT    NOT NULL DEFAULT 'https',
            checked      INTEGER NOT NULL DEFAULT 1,
            chain        TEXT    NOT NULL DEFAULT '[]'
        );
        CREATE INDEX idx_links_run ON links (run_id, source_url);
        """,
        """
        CREATE TABLE issues (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            fingerprint  TEXT    NOT NULL,
            type         TEXT    NOT NULL,
            severity     TEXT    NOT NULL,
            source_url   TEXT,
            target_url   TEXT    NOT NULL,
            final_url    TEXT,
            status       INTEGER,
            reason       TEXT,
            element      TEXT,
            attribute    TEXT,
            link_text    TEXT,
            chain        TEXT    NOT NULL DEFAULT '[]',
            found_at     TEXT    NOT NULL,
            UNIQUE (run_id, fingerprint)
        );
        CREATE INDEX idx_issues_fingerprint ON issues (fingerprint);
        CREATE INDEX idx_issues_run ON issues (run_id, severity, type);
        """,
    ),
    2: (
        # v0.1 -> v0.1.1: richer counters and a free-form notes column.
        "ALTER TABLE runs ADD COLUMN notes TEXT",
        "ALTER TABLE runs ADD COLUMN fragment_only INTEGER NOT NULL DEFAULT 0",
    ),
}


class Storage:
    """One SQLite database holding every run."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path = database_path(path)
        ensure_parent(self.path)
        self._local = threading.local()
        self._lock = threading.Lock()
        try:
            self.migrate()
        except BaseException:  # a half-open handle would outlive this failed constructor
            self.close()
            raise

    # ------------------------------------------------------------ connections
    @property
    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        conn = self.connection
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn.cursor()
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def write_from_thread(self, func: Callable[..., None], *args: Any) -> None:
        """Run a write in the calling thread, then release that thread's connection.

        Connections are thread-local; the crawler flushes from a worker thread in the
        default executor, and a pooled worker would otherwise hold an open database
        handle for as long as it lives (Python warns about it at shutdown).
        """
        try:
            func(*args)
        finally:
            self.close()

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------ migrations
    @property
    def schema_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def migrate(self) -> list[int]:
        """Bring the schema up to date; returns the versions that were applied.

        Statements are executed one at a time (``executescript`` would commit
        implicitly and break the transaction) so a half-applied migration cannot
        leave the database on a newer ``user_version`` than its tables.
        """
        applied: list[int] = []
        with self._lock:
            conn = self.connection
            current = self.schema_version
            if current > SCHEMA_VERSION:
                msg = (
                    f"{self.path} was written by a newer Linkkeep (schema v{current} > v{SCHEMA_VERSION}). "
                    "Upgrade Linkkeep or point --db at a different file."
                )
                raise RuntimeError(msg)
            for version in range(current + 1, SCHEMA_VERSION + 1):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in MIGRATIONS.get(version, ()):
                        for piece in _split_statements(statement):
                            conn.execute(piece)
                    conn.execute(f"PRAGMA user_version = {version}")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                applied.append(version)
        return applied

    @property
    def tables(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {str(row["name"]) for row in rows}

    def has_runs_table(self) -> bool:
        row = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ write paths
    def create_run(
        self,
        site: str,
        seeds: Sequence[str],
        config: dict[str, Any],
        started_at: dt.datetime | None = None,
    ) -> int:
        started = started_at or dt.datetime.now(dt.timezone.utc)
        with self.transaction() as cursor:
            cursor.execute(
                "INSERT INTO runs (site, started_at, status, seeds, config) VALUES (?, ?, ?, ?, ?)",
                (
                    site,
                    started.isoformat(),
                    RunStatus.RUNNING.value,
                    json.dumps(list(seeds)),
                    json.dumps(config),
                ),
            )
            return int(cursor.lastrowid or 0)

    def save_run_data(
        self,
        run_id: int,
        pages: Sequence[PageRecord],
        links: Sequence[LinkRecord],
        issues: Sequence[Issue],
        counts: Counts,
        status: RunStatus,
        finished_at: dt.datetime | None,
        notes: Sequence[str] = (),
        duration_ms: int | None = None,
    ) -> None:
        """Replace all rows of one run (idempotent, so it doubles as the flush path)."""
        with self.transaction() as cursor:
            cursor.execute("DELETE FROM pages WHERE run_id = ?", (run_id,))
            cursor.execute("DELETE FROM links WHERE run_id = ?", (run_id,))
            cursor.execute("DELETE FROM issues WHERE run_id = ?", (run_id,))
            for page in pages:
                cursor.execute(
                    "INSERT OR REPLACE INTO pages (run_id, url, depth, status, final_url, duration_ms,"
                    " content_type, size_bytes, error, crawlable, links_found, links_checked)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        page.url,
                        page.depth,
                        page.status,
                        page.final_url,
                        page.duration_ms,
                        page.content_type,
                        page.size_bytes,
                        page.error,
                        int(page.crawlable),
                        page.links_found,
                        page.links_checked,
                    ),
                )
            for link in links:
                cursor.execute(
                    "INSERT INTO links (run_id, source_url, target_url, kind, element, attribute, link_text,"
                    " final_url, status, duration_ms, error, scheme, checked, chain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        link.source_url,
                        link.target_url,
                        link.kind.value,
                        link.element,
                        link.attribute,
                        link.link_text,
                        link.final_url,
                        link.status,
                        link.duration_ms,
                        link.error,
                        link.scheme,
                        int(link.checked),
                        json.dumps([hop.as_dict() for hop in link.redirect_chain]),
                    ),
                )
            for issue in issues:
                cursor.execute(
                    "INSERT OR IGNORE INTO issues (run_id, fingerprint, type, severity, source_url, target_url,"
                    " final_url, status, reason, element, attribute, link_text, chain, found_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        issue.fingerprint,
                        issue.code,
                        issue.severity.value,
                        issue.source_url,
                        issue.target_url,
                        issue.final_url,
                        issue.status,
                        issue.reason,
                        issue.element,
                        issue.attribute,
                        issue.link_text,
                        json.dumps([hop.as_dict() for hop in issue.redirect_chain]),
                        issue.found_at.isoformat(),
                    ),
                )
            self._update_run_row(cursor, run_id, counts, status, finished_at, notes, duration_ms)

    def _update_run_row(
        self,
        cursor: sqlite3.Cursor,
        run_id: int,
        counts: Counts,
        status: RunStatus,
        finished_at: dt.datetime | None,
        notes: Sequence[str],
        duration_ms: int | None,
    ) -> None:
        cursor.execute(
            "UPDATE runs SET pages=?, links=?, checked=?, errors=?, warnings=?, infos=?, skipped=?,"
            " excluded=?, robots_blocked=?, fragment_only=?, by_type=?, status=?, finished_at=?,"
            " notes=COALESCE(?, notes), duration_ms=COALESCE(?, duration_ms) WHERE id=?",
            (
                counts.pages,
                counts.links,
                counts.checked,
                counts.errors,
                counts.warnings,
                counts.infos,
                counts.skipped_scheme,
                counts.excluded,
                counts.robots_blocked,
                counts.fragment_only,
                json.dumps(counts.by_type, sort_keys=True),
                status.value,
                finished_at.isoformat() if finished_at else None,
                "; ".join(notes) if notes else None,
                duration_ms,
                run_id,
            ),
        )

    def update_run_status(self, run_id: int, status: RunStatus, notes: Sequence[str] = ()) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE runs SET status = ?, finished_at = COALESCE(finished_at, ?), notes = COALESCE(?, notes) WHERE id = ?",
                (
                    status.value,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    "; ".join(notes) if notes else None,
                    run_id,
                ),
            )

    # ------------------------------------------------------------ read paths
    def get_run(self, run_id: int) -> RunRecord | None:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def list_runs(self, *, site: str | None = None, limit: int = 50) -> list[RunRecord]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if site:
            sql += " WHERE site = ?"
            params.append(site)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, limit))
        rows = self.connection.execute(sql, params).fetchall()
        return [_row_to_run(row) for row in rows]

    def latest_run(self, site: str | None = None) -> RunRecord | None:
        runs = self.list_runs(site=site, limit=1)
        return runs[0] if runs else None

    def issues_for_run(self, run_id: int) -> list[Issue]:
        rows = self.connection.execute(
            "SELECT * FROM issues WHERE run_id = ? ORDER BY severity, type, source_url, target_url",
            (run_id,),
        ).fetchall()
        return [_row_to_issue(row) for row in rows]

    def pages_for_run(self, run_id: int) -> list[PageRecord]:
        rows = self.connection.execute(
            "SELECT * FROM pages WHERE run_id = ? ORDER BY depth, url", (run_id,)
        ).fetchall()
        return [_row_to_page(row) for row in rows]

    def links_for_run(self, run_id: int) -> list[LinkRecord]:
        rows = self.connection.execute(
            "SELECT * FROM links WHERE run_id = ? ORDER BY source_url, target_url", (run_id,)
        ).fetchall()
        return [_row_to_link(row) for row in rows]

    def first_seen(
        self, site: str, fingerprints: Iterable[str], *, before_run: int | None = None
    ) -> dict[str, dt.datetime]:
        """Earliest run start where each fingerprint was reported for this site."""
        wanted = list(fingerprints)
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        sql = (
            "SELECT i.fingerprint AS fp, MIN(r.started_at) AS first_seen"
            " FROM issues i JOIN runs r ON r.id = i.run_id"
            f" WHERE r.site = ? AND i.fingerprint IN ({placeholders})"
        )
        params: list[Any] = [site, *wanted]
        if before_run is not None:
            sql += " AND i.run_id < ?"
            params.append(before_run)
        sql += " GROUP BY i.fingerprint"
        rows = self.connection.execute(sql, params).fetchall()
        out: dict[str, dt.datetime] = {}
        for row in rows:
            raw = row["first_seen"]
            if raw:
                out[str(row["fp"])] = dt.datetime.fromisoformat(str(raw))
        return out

    def run_ids_for_site(self, site: str) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM runs WHERE site = ? ORDER BY id", (site,)
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def counts(self, run_id: int) -> Counts:
        run = self.get_run(run_id)
        return run.counts if run else Counts()

    def sites(self) -> list[str]:
        rows = self.connection.execute("SELECT DISTINCT site FROM runs ORDER BY site").fetchall()
        return [str(row["site"]) for row in rows]

    def prune(self, *, older_than: dt.datetime, keep_last: int = 1) -> list[int]:
        """Delete runs started before ``older_than``.

        The ``keep_last`` newest runs *per site* are never deleted, so history is
        always available for the next diff - even when every run is old.
        """
        ordered = self.connection.execute(
            "SELECT id, site, started_at FROM runs ORDER BY site, id DESC"
        ).fetchall()
        protected: set[int] = set()
        per_site: dict[str, int] = {}
        for row in ordered:
            site = str(row["site"])
            seen = per_site.get(site, 0)
            if seen < keep_last:
                protected.add(int(row["id"]))
                per_site[site] = seen + 1
        doomed = [
            int(row["id"])
            for row in ordered
            if int(row["id"]) not in protected and _parse_iso(str(row["started_at"])) < older_than
        ]
        if not doomed:
            return []
        placeholders = ",".join("?" for _ in doomed)
        with self.transaction() as cursor:
            for table in ("pages", "links", "issues"):
                cursor.execute(f"DELETE FROM {table} WHERE run_id IN ({placeholders})", doomed)
            cursor.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", doomed)
        return sorted(doomed)

    def mark_stale_runs_interrupted(self, *, after: dt.datetime | None = None) -> int:
        """A crashed process leaves ``status='running'`` rows; clean them up.

        Called before a crawl so ``linkkeep runs`` never shows a phantom run that is
        still going.  Returns the number of rows updated.
        """
        cutoff = (after or dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat()
        row = self.connection.execute(
            "SELECT id FROM runs WHERE status = 'running' AND started_at < ?", (cutoff,)
        ).fetchall()
        if not row:
            return 0
        ids = [int(item["id"]) for item in row]
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as cursor:
            cursor.execute(
                f"UPDATE runs SET status = ?, finished_at = ?, notes = COALESCE(notes, ?) WHERE id IN ({placeholders})",
                [
                    RunStatus.INTERRUPTED.value,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    "marked interrupted: no process left it open",
                    *ids,
                ],
            )
        return len(ids)

    def vacuum(self) -> None:
        self.connection.execute("VACUUM")

    # ------------------------------------------------------------ helpers
    def report_rows(
        self, run_id: int
    ) -> tuple[RunRecord, list[Issue], list[PageRecord], list[LinkRecord]]:
        run = self.get_run(run_id)
        if run is None:
            msg = f"no run with id {run_id} in {self.path}"
            raise KeyError(msg)
        return (
            run,
            self.issues_for_run(run_id),
            self.pages_for_run(run_id),
            self.links_for_run(run_id),
        )


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    started = dt.datetime.fromisoformat(str(row["started_at"]))
    finished = row["finished_at"]
    counts = Counts(
        pages=int(row["pages"] or 0),
        links=int(row["links"] or 0),
        checked=int(row["checked"] or 0),
        skipped_scheme=int(row["skipped"] or 0),
        fragment_only=int(row["fragment_only"] or 0) if "fragment_only" in set(row.keys()) else 0,
        excluded=int(row["excluded"] or 0),
        robots_blocked=int(row["robots_blocked"] or 0),
        errors=int(row["errors"] or 0),
        warnings=int(row["warnings"] or 0),
        infos=int(row["infos"] or 0),
        by_type=_loads(row["by_type"], {}),
    )
    keys = row.keys()
    return RunRecord(
        id=int(row["id"]),
        site=str(row["site"]),
        started_at=to_utc(started),
        finished_at=to_utc(dt.datetime.fromisoformat(str(finished))) if finished else None,
        status=RunStatus(str(row["status"] or "completed")),
        seeds=_loads(row["seeds"], []),
        counts=counts,
        config=_loads(row["config"], {}),
        notes=str(row["notes"]) if "notes" in keys and row["notes"] else None,
    )


def _row_to_issue(row: sqlite3.Row) -> Issue:
    chain = [RedirectHop(**hop) for hop in _loads(row["chain"], [])]
    found_at = row["found_at"]
    return Issue(
        type=IssueType(str(row["type"])),
        fingerprint=str(row["fingerprint"]),
        source_url=row["source_url"],
        target_url=str(row["target_url"]),
        final_url=row["final_url"],
        status=row["status"],
        reason=row["reason"],
        element=row["element"],
        attribute=row["attribute"],
        link_text=row["link_text"],
        redirect_chain=chain,
        found_at=to_utc(dt.datetime.fromisoformat(str(found_at))),
    )


def _row_to_page(row: sqlite3.Row) -> PageRecord:
    return PageRecord(
        run_id=int(row["run_id"]),
        url=str(row["url"]),
        depth=int(row["depth"] or 0),
        status=row["status"],
        final_url=row["final_url"],
        duration_ms=row["duration_ms"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        error=row["error"],
        crawlable=bool(row["crawlable"]),
        links_found=int(row["links_found"] or 0),
        links_checked=int(row["links_checked"] or 0),
    )


def _row_to_link(row: sqlite3.Row) -> LinkRecord:
    return LinkRecord(
        run_id=int(row["run_id"]),
        source_url=str(row["source_url"]),
        target_url=str(row["target_url"]),
        kind=LinkKind(str(row["kind"])),
        element=row["element"],
        attribute=row["attribute"],
        link_text=row["link_text"],
        final_url=row["final_url"],
        status=row["status"],
        duration_ms=row["duration_ms"],
        error=row["error"],
        scheme=str(row["scheme"] or "https"),
        checked=bool(row["checked"]),
        redirect_chain=[RedirectHop(**hop) for hop in _loads(row["chain"], [])],
    )


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def severity_order(issue: Issue) -> int:
    return {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}[issue.severity]


def fingerprint_for(issue_type: IssueType, source_url: str | None, target_url: str) -> str:
    """Re-exported for callers that build issues outside the detector module."""
    return make_fingerprint(issue_type, source_url, target_url)


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Storage", "fingerprint_for", "severity_order"]


def _split_statements(script: str) -> list[str]:
    """Split a migration blob into single statements (blank ones dropped)."""
    return [piece.strip() for piece in script.split(";") if piece.strip()]


def _parse_iso(value: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - defensive against hand-edited databases
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return to_utc(moment)
