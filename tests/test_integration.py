"""End-to-end: the real CLI, the real fetcher, the real SQLite file, a real HTTP server.

The server is :mod:`linkkeep.fixture_site`, served from ``127.0.0.1`` on an ephemeral
port, so nothing here leaves the machine.  It is deliberately messy in the ways real
sites are: a redirect loop, a 500, a 410, a flaky endpoint, ``robots.txt`` disallowing a
directory, ``<base href>``, srcset images, fragments, and links that point at a
*different* origin (``localhost`` vs ``127.0.0.1``) so external-link checking is covered.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import pytest

from linkkeep import cli
from linkkeep.config import DEFAULT_USER_AGENT
from linkkeep.fixture_site import FixtureServer, running_site
from linkkeep.storage import Storage

pytestmark = pytest.mark.integration


def path_of(url: str | None) -> str:
    """``http://localhost:8080/x`` -> ``/x`` - comparable across the 127.0.0.1/localhost split."""
    import re as _re

    return _re.sub(r"^https?://[^/]+", "", url or "") or "/"


# --------------------------------------------------------------------------------- plumbing
def run_cli(args: list[str]) -> tuple[int, str]:
    """Invoke the console script exactly as a user would, capturing everything it prints."""
    buffer = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, buffer.getvalue()


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureServer]:
    with running_site(variant="broken") as server:
        yield server


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "history.sqlite"


def crawl(
    server: FixtureServer,
    db: Path,
    *extra: str,
    fmt: str = "text",
    output: Path | None = None,
) -> tuple[int, str]:
    args = [
        "--db",
        str(db),
        "crawl",
        server.url + "/",
        "--allow-private",
        "--format",
        fmt,
        "--max-pages",
        "60",
        "--rate",
        "50",
        "--timeout",
        "5",
        "--concurrency",
        "8",
        *extra,
    ]
    if output is not None:
        args += ["--output", str(output)]
    return run_cli(args)


def codes(db: Path, run_id: int | None = None) -> list[str]:
    storage = Storage(db)
    try:
        run = (run_id and storage.get_run(run_id)) or storage.list_runs()[0]
        assert run is not None
        return [issue.code for issue in storage.issues_for_run(run.id)]
    finally:
        storage.close()


def issues(db: Path) -> list[dict[str, str]]:
    storage = Storage(db)
    try:
        run = storage.list_runs()[0]
        assert run is not None
        return [issue.public_dict() for issue in storage.issues_for_run(run.id)]
    finally:
        storage.close()


# --------------------------------------------------------------------------------- the crawl
def test_crawling_the_fixture_site_finds_every_planted_problem(
    site: FixtureServer, db: Path
) -> None:
    code, output = crawl(site, db)
    assert code == 1  # errors present, --fail-on defaults to error
    assert "errors" in output

    found = set(codes(db))
    assert {
        "broken",
        "redirect_loop",
        "unreachable",
        "redirect_temporary_permanent_internal",
    } <= found

    detail = {(item["code"], path_of(item["target_url"])) for item in issues(db)}
    assert ("broken", "/gone") in detail
    assert ("broken", "/error") in detail
    assert ("broken", "/missing") in detail
    assert ("broken", "/ext-broken") in detail  # external link, checked with HEAD only
    assert ("redirect_loop", "/loop-a") in detail
    assert ("redirect_temporary_permanent_internal", "/temp-news") in detail

    storage = Storage(db)
    try:
        run = storage.list_runs()[0]
        assert run is not None
        assert run.status.value == "completed"
        assert run.counts.pages >= 10
        assert run.counts.checked >= 20
        assert run.counts.skipped_scheme == 4  # mailto x2, tel, javascript
        assert run.counts.robots_blocked == 1  # /private/secret
        assert run.counts.excluded == 0
    finally:
        storage.close()


def test_the_real_fetcher_sends_a_polite_identifying_user_agent(
    site: FixtureServer, db: Path
) -> None:
    crawl(site, db)
    assert site.seen_user_agents == {DEFAULT_USER_AGENT}
    assert DEFAULT_USER_AGENT.startswith("Linkkeep/0.1.0 (+http")
    site.reset_counters()
    crawl(site, db)
    # a 404 that is both a queued page and a link target must not be fetched twice
    assert site.hits("/gone") <= 2
    assert site.hits("/") == 1  # the seed itself is fetched exactly once


def test_robots_txt_is_respected_by_default_and_can_be_overridden(
    site: FixtureServer, tmp_path: Path
) -> None:
    with running_site(variant="broken") as server:
        first = tmp_path / "with-robots.sqlite"
        crawl(server, first)
        storage = Storage(first)
        try:
            pages = {page.url for page in storage.pages_for_run(storage.list_runs()[0].id)}  # type: ignore[union-attr]
        finally:
            storage.close()
        assert f"{server.url}/private/secret" not in pages

        second = tmp_path / "no-robots.sqlite"
        code, output = crawl(server, second, "--ignore-robots")
        assert "robots.txt is being ignored" in output
        storage = Storage(second)
        try:
            run = storage.list_runs()[0]
            assert run is not None
            pages = {page.url for page in storage.pages_for_run(run.id)}
            assert f"{server.url}/private/secret" in pages
            assert run.counts.robots_blocked == 0
        finally:
            storage.close()
        assert code == 1


def test_fragments_are_only_checked_when_asked(site: FixtureServer, db: Path) -> None:
    code, _ = crawl(site, db)
    assert "missing_fragment" not in codes(db)  # not checked without the flag

    with running_site(variant="broken") as other:
        second = db.with_name("fragments.sqlite")
        crawl(other, second, "--check-fragments")
        found = [item for item in issues(second) if item["code"] == "missing_fragment"]
        assert len(found) == 1
        assert found[0]["target_url"].endswith("/blog/post-1#nope")
        assert "nope" in found[0]["reason"]
    assert code == 1


def test_sitemap_adds_pages_that_no_page_links_to(site: FixtureServer, db: Path) -> None:
    _, output = crawl(site, db, "--sitemap")
    storage = Storage(db)
    try:
        run = storage.list_runs()[0]
        assert run is not None
        pages = {page.url for page in storage.pages_for_run(run.id)}
    finally:
        storage.close()
    assert f"{site.url}/blog/post-3" in pages
    assert "sitemap" in output.lower()


def test_max_depth_limits_the_walk(site: FixtureServer, db: Path) -> None:
    crawl(site, db, "--max-depth", "1")
    storage = Storage(db)
    try:
        run = storage.list_runs()[0]
        assert run is not None
        depths = {page.depth for page in storage.pages_for_run(run.id)}
    finally:
        storage.close()
    assert depths == {0, 1}


def test_exclude_pattern_skips_a_whole_directory(site: FixtureServer, db: Path) -> None:
    crawl(site, db, "--exclude", "re:/blog/")
    storage = Storage(db)
    try:
        run = storage.list_runs()[0]
        assert run is not None
        pages = {page.url for page in storage.pages_for_run(run.id)}
    finally:
        storage.close()
    assert not any("/blog/" in url for url in pages)
    assert f"{site.url}/" in pages
    assert run.counts.excluded >= 2


def test_base_href_and_srcset_are_resolved_against_the_page(
    site: FixtureServer, db: Path, tmp_path: Path
) -> None:
    html = tmp_path / "report.html"
    crawl(site, db, "--format", "html", "--output", html)
    text = html.read_text(encoding="utf-8")
    assert html.is_file()
    assert len(text) > 2000
    # <base href="/deep/"> on /careers makes "level2" resolve to /deep/level2
    storage = Storage(db)
    try:
        run = storage.list_runs()[0]
        assert run is not None
        targets = {link.target_url for link in storage.links_for_run(run.id)}
    finally:
        storage.close()
    assert f"{site.url}/deep/level2" in targets
    assert f"{site.url}/img/responsive-2x.png" in targets  # from srcset
    assert f"{site.url}/img/logo.png" in targets


# ----------------------------------------------------------------------------- all formats
@pytest.mark.parametrize("fmt", ["text", "markdown", "html", "json", "junit"])
def test_every_format_is_written_and_well_formed(
    site: FixtureServer, db: Path, tmp_path: Path, fmt: str
) -> None:
    target = tmp_path / f"report.{fmt}"
    code, output = crawl(site, db, "--format", fmt, "--output", str(target))
    assert code == 1
    assert "wrote" in output
    body = target.read_text(encoding="utf-8")
    assert body, target
    if fmt == "json":
        payload = json.loads(body)
        assert payload["schema_version"] == 1
        assert payload["site"] == f"{site.url}/"
        assert {item["code"] for item in payload["issues"]} >= {"broken", "redirect_loop"}
        assert len(payload["pages"]) >= 10
    elif fmt == "junit":
        root = ET.fromstring(body)
        assert int(root.attrib["failures"]) >= 5
        assert int(root.attrib["tests"]) == len(root.findall(".//testcase"))
    elif fmt == "html":
        assert body.startswith("<!doctype html>")
        assert "<script>" in body
        assert not re.search(r'src="http|href="http://|href="https://', body)
    elif fmt == "markdown":
        assert body.startswith("## Linkkeep:")
        assert "| :x: | `broken`" in body
    else:
        assert "ISSUES" in body
        assert "[ERROR] broken" in body


def test_a_clean_site_exits_zero(tmp_path: Path) -> None:
    """The same tool, on a site with nothing wrong, must exit 0 with no report noise."""
    with running_site(variant="fixed") as server:
        code, output = run_cli(
            [
                "--db",
                str(tmp_path / "clean.sqlite"),
                "crawl",
                server.url + "/",
                "--allow-private",
                "--no-external",
                "--format",
                "text",
                "--exclude",
                "re:/missing|/gone|/error|/loop|/flaky|/redirect|relative-sibling",
            ]
        )
    assert code == 0, output
    assert "0 error" in output
    assert "blocked_by_robots" in output  # reported as info, but never fails the build


# ---------------------------------------------------------------------- history + diff (CI)
def test_diff_after_a_fix_exits_zero_and_lists_fixed(tmp_path: Path) -> None:
    database = tmp_path / "ci.sqlite"
    with running_site(variant="broken") as server:
        crawl(server, database)
        server.set_variant("fixed")
        code, output = run_cli(
            [
                "--db",
                str(database),
                "crawl",
                server.url + "/",
                "--allow-private",
                "--format",
                "none",
            ]
        )
        assert code == 1  # the fixed site still has some problems
        code, output = run_cli(["--db", str(database), "diff", "-q"])
        assert code == 0  # nothing NEW, so CI would pass
        assert "0 new" in output
        assert "fixed" in output

        code, output = run_cli(["--db", str(database), "diff", "--format", "markdown"])
        assert code == 0
        assert "### Changes since the previous crawl" in output
        assert "No new problems" in output
        assert ":checkered_flag:" in output

        # and the report command can render the same diff for humans
        storage = Storage(database)
        try:
            newest = storage.list_runs()[0]
            assert newest is not None
            run_id = newest.id
        finally:
            storage.close()
        code, output = run_cli(
            [
                "--db",
                str(database),
                "report",
                str(run_id),
                "--format",
                "text",
                "--diff-with-previous",
            ]
        )
        assert code == 0
        assert "DIFF" in output
        assert "FIXED" in output


def test_diff_when_a_link_breaks_later_exits_one(tmp_path: Path) -> None:
    database = tmp_path / "regression.sqlite"
    with running_site(variant="fixed") as server:
        crawl(server, database)
        server.set_variant("broken")
        crawl(server, database)
        code, output = run_cli(["--db", str(database), "diff"])
        assert code == 1  # a regression: CI must fail
        assert "NEW" in output
        assert "/gone" in output
        # the fingerprint is stable across runs, so PERSISTING is detected, not "new"
        assert "PERSISTING" in output
        code, _ = run_cli(["--db", str(database), "diff", "--fail-on", "never"])
        assert code == 0


def test_runs_and_prune_and_report_on_a_real_history(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite"
    with running_site(variant="broken") as server:
        crawl(server, database)
        code, output = run_cli(["--db", str(database), "runs"])
        assert code == 0
        assert f"{server.url}/" in output  # the whole site URL, never truncated
        assert "completed" in output

        code, output = run_cli(["--db", str(database), "runs", "--json"])
        payload = json.loads(output)
        assert payload[0]["counts"]["errors"] >= 3
        assert payload[0]["started_at"].startswith("2026") or payload[0]["started_at"]

        code, output = run_cli(
            ["--db", str(database), "report", str(payload[0]["id"]), "--format", "markdown"]
        )
        assert code == 0
        assert "## Linkkeep:" in output

        code, output = run_cli(["--db", str(database), "prune", "--older-than", "90d", "--yes"])
        assert code == 0
        assert "nothing older than 90d" in output

        # watch --once diffs against the previous run without keeping a process alive
        code, output = run_cli(
            [
                "--db",
                str(database),
                "watch",
                server.url + "/",
                "--allow-private",
                "--once",
                "--every",
                "1h",
                "-q",
            ]
        )
        assert code in (0, 1)
        assert "new" in output
        assert "persisting" in output


# ------------------------------------------------------------------ the manual server helper
def _get(url: str, *, timeout: float = 1.0) -> str:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "linkkeep-integration-test"})
    # urllib rather than httpx here: this test checks the server itself, not the client
    with urllib.request.urlopen(request, timeout=timeout) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


def _get_ready(url: str, *, within: float) -> str:
    """Poll until the server answers, so the test does not depend on process start-up speed."""
    import time

    deadline = time.monotonic() + within
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return _get(url)
        except Exception as exc:  # any failure means "not yet"
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"{url} never answered: {last!r}")


@pytest.mark.skipif(os.name == "nt", reason="SIGINT to a child process is Windows-specific")
def test_serving_the_fixture_site_as_a_module_answers_and_stops_cleanly(tmp_path: Path) -> None:
    """`python -m linkkeep.fixture_site` is a supported entry point, so it must really serve.

    A server that binds a socket and never calls ``accept`` looks alive (connections are
    accepted by the kernel) but every request times out - which is exactly the failure this
    test catches.
    """
    import signal
    import socket
    import subprocess
    from contextlib import closing

    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    log = tmp_path / "server.log"
    with log.open("w", encoding="utf-8") as handle:
        # our own interpreter with a fixed argument list, so no shell/quoting risk
        process = subprocess.Popen(
            [sys.executable, "-m", "linkkeep.fixture_site", "--port", str(port)],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            assert "Acme Widgets" in _get_ready(f"{base}/", within=20.0)
            assert "User-agent" in _get_ready(f"{base}/robots.txt", within=5.0)
        finally:
            process.send_signal(signal.SIGINT)
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - Ctrl+C ignored
                process.kill()
                process.wait(timeout=5)
                raise

    text = log.read_text(encoding="utf-8")
    assert code == 0, text
    assert f"serving broken fixture site at {base}" in text
    assert "Traceback" not in text
    assert "stopped" in text
