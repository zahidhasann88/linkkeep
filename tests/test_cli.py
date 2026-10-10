"""Command line interface: flags, exit codes, files written, friendly errors.

The HTTP layer is always replaced by :class:`~tests.conftest.FakeFetcher` (via
``monkeypatch.setattr(cli, "HttpxFetcher", ...)``), so these tests exercise the real
crawler, storage and renderers without a single socket.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import signal
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.conftest import BASE, NOW, FakeFetcher, issue, page
from typer.testing import CliRunner

from linkkeep import __version__, cli
from linkkeep.cli import EXIT_CRAWL_FAILED, EXIT_ISSUES, EXIT_OK, EXIT_USAGE, app
from linkkeep.config import Config
from linkkeep.crawler import CrawlReport
from linkkeep.models import Counts, LinkRecord, RunStatus, Severity
from linkkeep.storage import Storage

SITE = BASE
runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_sigint():  # type: ignore[no-untyped-def]
    """``run_crawl`` installs a SIGINT handler; put pytest's own back afterwards."""
    before = signal.getsignal(signal.SIGINT)
    yield
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, before)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "history.sqlite"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def invoke(args: list[str], *, db: Path | None = None, input: str | None = None):  # type: ignore[no-untyped-def]
    argv = ["--db", str(db), *args] if db is not None else list(args)
    result = CliRunner().invoke(app, argv, input=input, color=False)
    # `main()` closes the databases it opened; invoking the app directly does not
    cli.close_open_storages()
    return result


def invoke_main(args: list[str]) -> tuple[int, str]:
    """Run the console-script entry point, returning its exit code and stdout."""
    buffer = io.StringIO()
    code = EXIT_OK
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, buffer.getvalue()


def stub_network(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, dict[str, Any]],
    *,
    default_status: int | None = 404,
    allow_unknown: bool = False,
) -> type[FakeFetcher]:
    """Replace the real fetcher with a route-table fake for the duration of a test."""
    seen: list[FakeFetcher] = []

    class StubbedFetcher(FakeFetcher):
        def __init__(self, **_kwargs: Any) -> None:
            del _kwargs  # the crawler's tuning knobs are irrelevant to the fake
            super().__init__(routes, default_status=default_status, allow_unknown=allow_unknown)
            seen.append(self)

    StubbedFetcher.instances = seen  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "HttpxFetcher", StubbedFetcher)
    return StubbedFetcher


def history(db: Path) -> Storage:
    """Open the database under test; the autouse fixture closes it so pytest stays quiet."""
    store = Storage(db)
    _OPEN_STORAGES.append(store)
    return store


_OPEN_STORAGES: list[Storage] = []


@pytest.fixture(autouse=True)
def _close_storages() -> Any:
    yield
    for store in _OPEN_STORAGES:
        with contextlib.suppress(Exception):
            store.close()
    _OPEN_STORAGES.clear()


def stored_run(
    storage: Storage,
    started: dt.datetime,
    items: list[Any],
    *,
    site: str = SITE,
    status: RunStatus = RunStatus.COMPLETED,
    links: list[LinkRecord] | None = None,
) -> int:
    run_id = storage.create_run(site, [f"{site}/"], {}, started)
    counts = Counts(
        pages=4,
        links=len(links or []),
        checked=len(links or []),
        errors=sum(1 for item in items if item.severity is Severity.ERROR),
        warnings=sum(1 for item in items if item.severity is Severity.WARNING),
        infos=sum(1 for item in items if item.severity is Severity.INFO),
    )
    storage.save_run_data(
        run_id, [], links or [], items, counts, status, started + dt.timedelta(seconds=5)
    )
    return run_id


# ------------------------------------------------------------------------------ misc
def test_version_flag() -> None:
    result = invoke(["--version"])
    assert result.exit_code == EXIT_OK
    assert result.stdout.strip() == f"linkkeep {__version__}"


def test_no_arguments_prints_help() -> None:
    result = invoke([])
    assert result.exit_code == EXIT_USAGE  # typer's `no_args_is_help` exits 2
    for command in ("init", "crawl", "runs", "diff", "report", "watch", "prune"):
        assert command in result.stdout


def test_help_lists_every_command() -> None:
    result = invoke(["--help"])
    assert result.exit_code == EXIT_OK
    assert "Crawl a site, find broken links" in result.stdout


def test_unknown_command_is_a_usage_error() -> None:
    result = invoke(["nope"])
    assert result.exit_code == EXIT_USAGE


def test_each_command_has_help() -> None:
    for command in ("init", "crawl", "runs", "diff", "report", "watch", "prune"):
        result = invoke([command, "--help"])
        assert result.exit_code == EXIT_OK, command
        assert len(result.stdout) > 100, command


# ------------------------------------------------------------------------------ init
def test_init_writes_a_config_file(workdir: Path) -> None:
    result = invoke(["init"])
    assert result.exit_code == EXIT_OK
    written = workdir / "linkkeep.yaml"
    assert written.is_file()
    text = written.read_text(encoding="utf-8")
    assert "site:" in text
    assert "user_agent:" in text
    assert "linkkeep crawl" in result.stdout
    # what we print must be loadable, or the quickstart is a lie
    Config.from_dict(yaml.safe_load(text))


def test_init_refuses_to_clobber(workdir: Path) -> None:
    (workdir / "linkkeep.yaml").write_text("site: https://keep.me\n", encoding="utf-8")
    code, output = invoke_main(["init"])
    assert code == EXIT_USAGE
    assert "already exists" in output
    assert "keep.me" in (workdir / "linkkeep.yaml").read_text(encoding="utf-8")


def test_init_force_and_custom_path(workdir: Path) -> None:
    result = invoke(["init", "--path", "nested/linkkeep.yml", "--force"])
    assert result.exit_code == EXIT_OK
    assert (workdir / "nested" / "linkkeep.yml").is_file()


# ------------------------------------------------------------------------------ crawl
def test_crawl_clean_site_exits_zero(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        f"{SITE}/": page('<a href="/about">About</a>'),
        f"{SITE}/about": page('<a href="/">Home</a>'),
    }
    stub_network(monkeypatch, routes, allow_unknown=True, default_status=200)
    result = invoke(["crawl", f"{SITE}/"], db=db)
    assert result.exit_code == EXIT_OK
    assert "No issues found" in result.stdout
    assert "0 error" in result.stdout
    storage = history(db)
    assert [run.status for run in storage.list_runs()] == [RunStatus.COMPLETED]


def test_crawl_reports_broken_link_and_exits_one(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {f"{SITE}/": page('<a href="/gone">Gone</a>')}
    stub_network(monkeypatch, routes)
    result = invoke(["crawl", f"{SITE}/"], db=db)
    assert result.exit_code == EXIT_ISSUES
    assert "broken" in result.stdout
    assert f"{SITE}/gone" in result.stdout
    storage = history(db)
    run = storage.list_runs()[0]
    assert run.counts.errors == 1
    assert [i.code for i in storage.issues_for_run(run.id)] == ["broken"]


def test_fail_on_controls_the_exit_code(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {f"{SITE}/": page('<img src="http://cdn.example.com/x.png">')}
    stub_network(monkeypatch, routes, allow_unknown=True, default_status=200)
    # a warning only: the default threshold (error) passes
    assert invoke(["crawl", f"{SITE}/"], db=db).exit_code == EXIT_OK
    result = invoke(["crawl", f"{SITE}/", "--fail-on", "warning"], db=db)
    assert result.exit_code == EXIT_ISSUES
    assert invoke(["crawl", f"{SITE}/", "--fail-on", "never"], db=db).exit_code == EXIT_OK


def test_crawl_exit_code_three_when_nothing_can_be_crawled(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Dead(FakeFetcher):
        def __init__(self, **_kw: Any) -> None:
            super().__init__({}, allow_unknown=False, default_status=None)

    monkeypatch.setattr(cli, "HttpxFetcher", Dead)
    result = invoke(["crawl", f"{SITE}/"], db=db)
    assert result.exit_code == EXIT_CRAWL_FAILED
    assert "did not run" in result.stdout


def test_crawl_stores_a_run_and_no_store_does_not(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = {f"{SITE}/": page("<p>hi</p>")}
    stub_network(monkeypatch, routes, allow_unknown=True, default_status=200)
    assert invoke(["crawl", f"{SITE}/", "--no-store"], db=db).exit_code == EXIT_OK
    assert not db.exists()
    assert invoke(["crawl", f"{SITE}/"], db=db).exit_code == EXIT_OK
    assert db.exists()
    assert len(history(db).list_runs()) == 1


def test_crawl_uses_the_config_file_and_flags_override_it(
    workdir: Path, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Config] = []
    real_run_crawl = cli.run_crawl

    def spy(state, console, config, seeds, storage, *, interrupt=None):  # type: ignore[no-untyped-def]
        captured.append(config)
        return real_run_crawl(state, console, config, seeds, storage, interrupt=interrupt)

    monkeypatch.setattr(cli, "run_crawl", spy)
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    (workdir / "linkkeep.yaml").write_text(
        f"site: {SITE}/\ncrawl:\n  max_pages: 7\nrules:\n  follow_external: false\n",
        encoding="utf-8",
    )
    result = invoke(["crawl", "--max-pages", "3"], db=db)
    assert result.exit_code == EXIT_OK, result.stdout
    config = captured[0]
    assert config.site == f"{SITE}/"
    assert config.crawl.max_pages == 3  # flag wins over file
    assert config.rules.follow_external is False  # file survives the flag merge
    assert cli.CrawlFlags().to_overrides() == {}


def test_crawl_seed_comes_from_config_when_no_argument(
    workdir: Path, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    (workdir / "linkkeep.yaml").write_text(f"site: {SITE}/\n", encoding="utf-8")
    result = invoke(["crawl"], db=db)
    assert result.exit_code == EXIT_OK


def test_crawl_without_any_seed_is_a_usage_error(workdir: Path, db: Path) -> None:
    code, output = invoke_main(["--db", str(db), "crawl"])
    assert code == EXIT_USAGE
    assert "no seed URL" in output
    assert "linkkeep.yaml" in output
    assert "Traceback" not in output


def test_ignore_robots_prints_a_warning(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    result = invoke(["crawl", f"{SITE}/", "--ignore-robots"], db=db)
    assert "robots.txt is being ignored" in result.stdout


def test_every_crawl_flag_reaches_the_config(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Config] = []

    def fake(state, console, config, seeds, storage, *, interrupt=None):  # type: ignore[no-untyped-def]
        del state, console, seeds, storage, interrupt
        seen.append(config)
        return CrawlReport(site=SITE, seeds=[f"{SITE}/"])

    monkeypatch.setattr(cli, "run_crawl", fake)
    result = invoke(
        [
            "crawl",
            f"{SITE}/",
            "--max-pages",
            "5",
            "--max-depth",
            "1",
            "--concurrency",
            "2",
            "--rate",
            "1",
            "--timeout",
            "3",
            "--retries",
            "0",
            "--include",
            "glob:**/blog/**",
            "--exclude",
            "re:/private",
            "--sitemap",
            "--check-fragments",
            "--include-subdomains",
            "--allow-private",
            "--no-external",
            "--ignore-status",
            "429",
            "--slow-ms",
            "10",
            "--user-agent",
            "MyCrawler/1.0",
        ],
        db=db,
    )
    assert result.exit_code == EXIT_OK, result.stdout
    config = seen[0]
    assert config.crawl.max_pages == 5
    assert config.crawl.max_depth == 1
    assert config.crawl.concurrency == 2
    assert config.crawl.rate == 1.0
    assert config.crawl.timeout == 3.0
    assert config.crawl.retries == 0
    assert config.rules.include == ["glob:**/blog/**"]
    assert config.rules.exclude == ["re:/private"]
    assert config.rules.sitemap is True
    assert config.rules.check_fragments is True
    assert config.rules.include_subdomains is True
    assert config.rules.allow_private is True
    assert config.rules.follow_external is False
    assert config.rules.ignore_status == [429]
    assert config.detection.slow_ms == 10
    assert config.user_agent == "MyCrawler/1.0"


def test_unknown_format_and_fail_on_are_reported_cleanly(db: Path) -> None:
    code, output = invoke_main(["--db", str(db), "crawl", SITE, "--format", "pdf"])
    assert code == EXIT_USAGE
    assert "unknown --format" in output
    assert "pdf" in output
    code, output = invoke_main(["--db", str(db), "crawl", SITE, "--fail-on", "sometimes"])
    assert code == EXIT_USAGE
    assert "unknown --fail-on" in output


def test_crawl_writes_json_and_junit_files(
    db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_network(monkeypatch, {f"{SITE}/": page('<a href="/gone">x</a>')})
    json_path = tmp_path / "out.json"
    result = invoke(["crawl", f"{SITE}/", "--format", "json", "--output", str(json_path)], db=db)
    assert result.exit_code == EXIT_ISSUES
    assert "wrote" in result.stdout
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["issues"][0]["code"] == "broken"

    junit_path = tmp_path / "out.xml"
    result = invoke(["crawl", f"{SITE}/", "--format", "junit", "--output", str(junit_path)], db=db)
    assert result.exit_code == EXIT_ISSUES
    root = ET.fromstring(junit_path.read_text(encoding="utf-8"))
    assert root.attrib["failures"] == "1"


def test_crawl_format_none_prints_nothing_but_still_exits_one(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_network(monkeypatch, {f"{SITE}/": page('<a href="/gone">x</a>')})
    result = invoke(["crawl", f"{SITE}/", "--format", "none"], db=db)
    assert result.exit_code == EXIT_ISSUES
    assert "ISSUES" not in result.stdout


def test_missing_auth_env_var_is_warned_about(
    db: Path, monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    (workdir / "linkkeep.yaml").write_text(
        f"site: {SITE}/\nauth:\n  headers:\n    Authorization: env:LINKKEEP_DEFINITELY_UNSET\n",
        encoding="utf-8",
    )
    result = invoke(["crawl"], db=db)
    assert result.exit_code == EXIT_OK
    assert "LINKKEEP_DEFINITELY_UNSET is not set" in result.stdout
    assert "Authorization" in result.stdout
    # the value is never printed, so there is nothing to leak even when it is set
    monkeypatch.setenv("LINKKEEP_DEFINITELY_UNSET", "s3cret")
    again = invoke(["crawl", "--verbose"], db=db)
    assert "s3cret" not in again.stdout


def test_quiet_suppresses_the_report_body(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_network(monkeypatch, {f"{SITE}/": page('<a href="/gone">x</a>')})
    loud = invoke(["crawl", f"{SITE}/"], db=db)
    quiet = invoke(["crawl", f"{SITE}/", "-q"], db=db)
    assert "ISSUES" in loud.stdout
    assert "ISSUES" not in quiet.stdout
    assert "1 error" in quiet.stdout
    assert len(quiet.stdout.strip().splitlines()) < len(loud.stdout.strip().splitlines())


def test_verbose_adds_progress_lines(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    result = invoke(["crawl", f"{SITE}/", "--verbose"], db=db)
    assert "rate limits" in result.stdout


def test_private_seed_needs_the_flag(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_network(
        monkeypatch,
        {"http://127.0.0.1:8080/": page("<p>hi</p>")},
        allow_unknown=True,
        default_status=200,
    )
    code, output = invoke_main(["--db", str(db), "crawl", "http://127.0.0.1:8080/"])
    assert code == EXIT_USAGE
    assert "private" in output
    assert "--allow-private" in output
    assert (
        invoke(["crawl", "http://127.0.0.1:8080/", "--allow-private"], db=db).exit_code == EXIT_OK
    )


# ------------------------------------------------------------------------------- runs
def test_runs_on_an_empty_database_is_helpful(db: Path) -> None:
    result = invoke(["runs"], db=db)
    assert result.exit_code == EXIT_OK
    assert "no runs recorded yet" in result.stdout
    assert "linkkeep crawl" in result.stdout


def test_runs_lists_history(db: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW, [issue("broken")])
    stored_run(storage, NOW + dt.timedelta(days=1), [], site="https://other.test")
    result = invoke(["runs"], db=db)
    assert result.exit_code == EXIT_OK
    assert f"https://{SITE[8:]}" in result.stdout
    assert "https://other.test" in result.stdout
    assert "https://site.test" in result.stdout  # never truncated to "https://site"
    assert "2 run(s)" in result.stdout


def test_runs_can_filter_by_site_and_limit(db: Path) -> None:
    storage = history(db)
    for offset in range(3):
        stored_run(
            storage,
            NOW + dt.timedelta(days=offset),
            [],
            site=SITE if offset else "https://other.test",
        )
    assert "1 run(s)" in invoke(["runs", "--site", "https://other.test"], db=db).stdout
    assert "2 run(s)" in invoke(["runs", "--site", SITE], db=db).stdout
    assert "1 run(s)" in invoke(["runs", "--limit", "1"], db=db).stdout


def test_runs_json(db: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW, [issue("broken"), issue("slow")])
    payload = json.loads(invoke(["runs", "--json"], db=db).stdout)
    assert payload[0]["counts"]["errors"] == 1
    assert payload[0]["status"] == "completed"
    assert payload[0]["site"] == SITE
    assert payload[0]["db"].endswith("history.sqlite")


# ------------------------------------------------------------------------------- diff
def test_diff_between_two_runs(db: Path) -> None:
    storage = history(db)
    gone = issue("broken", target=f"{SITE}/gone", status=410)
    first = stored_run(storage, NOW, [gone, issue("broken", target=f"{SITE}/old", status=404)])
    second = stored_run(
        storage,
        NOW + dt.timedelta(days=1),
        [gone, issue("broken", target=f"{SITE}/new", status=500)],
    )
    result = invoke(["diff"], db=db)
    assert result.exit_code == EXIT_ISSUES  # a new broken link
    assert "NEW" in result.stdout
    assert "FIXED" in result.stdout
    assert "PERSISTING" in result.stdout
    assert f"{SITE}/new" in result.stdout
    assert "broken since" in result.stdout
    assert f"run #{second} vs run #{first}" in result.stdout


def test_diff_quiet_and_verbose(db: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW, [])
    stored_run(storage, NOW + dt.timedelta(days=1), [issue("broken")])
    quiet = invoke(["diff", "-q"], db=db)
    assert quiet.stdout.strip().splitlines()[-1] == f"{SITE}: 1 new, 0 fixed, 0 persisting"
    loud = invoke(["diff", "--verbose"], db=db)
    assert "compared run #1 with run #2" in loud.stdout


def test_diff_with_no_changes_exits_zero(db: Path) -> None:
    storage = history(db)
    only = issue("broken")
    stored_run(storage, NOW, [only])
    stored_run(storage, NOW + dt.timedelta(days=1), [only])
    result = invoke(["diff"], db=db)
    assert result.exit_code == EXIT_OK
    assert "PERSISTING" in result.stdout
    assert "NEW" in result.stdout
    assert "FIXED" in result.stdout
    empty = invoke(["diff", "-q"], db=db)
    assert empty.stdout.strip() == f"{SITE}: 0 new, 0 fixed, 1 persisting"


def test_diff_can_be_limited_to_a_site_and_write_a_file(db: Path, tmp_path: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW - dt.timedelta(days=2), [])  # this site has only one run
    stored_run(storage, NOW, [], site="https://other.test")
    stored_run(storage, NOW + dt.timedelta(days=1), [issue("broken")], site="https://other.test")
    target = tmp_path / "diff.md"
    result = invoke(
        ["diff", "--site", "https://other.test", "--format", "markdown", "--output", str(target)],
        db=db,
    )
    assert result.exit_code == EXIT_ISSUES
    assert "new problem" in target.read_text(encoding="utf-8")
    assert invoke(["diff", "--site", SITE], db=db).exit_code == EXIT_USAGE


def test_diff_with_unknown_ids_exits_two(db: Path) -> None:
    history(db).create_run(SITE, [], {}, NOW)
    result = invoke(["diff", "99", "98"], db=db)
    assert result.exit_code == EXIT_USAGE
    assert "no run with id 99" in result.stdout


def test_diff_fail_on_only_counts_new_issues(db: Path) -> None:
    storage = history(db)
    slow = issue("slow", target=f"{SITE}/x")
    stored_run(storage, NOW, [])
    stored_run(storage, NOW + dt.timedelta(days=1), [slow])
    assert invoke(["diff"], db=db).exit_code == EXIT_OK  # info < error
    assert invoke(["diff", "--fail-on", "info"], db=db).exit_code == EXIT_ISSUES


# ------------------------------------------------------------------------------ report
def test_report_exports_a_stored_run(db: Path, tmp_path: Path) -> None:
    storage = history(db)
    run_id = stored_run(storage, NOW, [issue("broken", target=f"{SITE}/gone", status=404)])
    result = invoke(["report", str(run_id)], db=db)
    assert result.exit_code == EXIT_OK
    assert result.stdout.startswith("## Linkkeep:")
    assert f"{SITE}/gone" in result.stdout

    html_path = tmp_path / "report.html"
    result = invoke(["report", str(run_id), "--format", "html", "--output", str(html_path)], db=db)
    assert "wrote" in result.stdout
    body = html_path.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert "<style>" in body
    assert f"{SITE}/gone" in body


def test_report_can_include_the_previous_diff(db: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW, [])
    second = stored_run(storage, NOW + dt.timedelta(days=1), [issue("broken")])
    result = invoke(["report", str(second), "--format", "text", "--diff-with-previous"], db=db)
    assert "DIFF" in result.stdout
    assert "NEW" in result.stdout
    first = storage.list_runs()[-1].id
    lonely = invoke(["report", str(first), "--format", "text", "--diff-with-previous"], db=db)
    assert "no earlier run" in lonely.stdout


def test_report_unknown_run_is_an_error(db: Path) -> None:
    history(db).create_run(SITE, [], {}, NOW)
    code, output = invoke_main(["--db", str(db), "report", "77"])
    assert code == EXIT_USAGE
    assert "no run with id 77" in output


def test_report_rejects_none_and_unknown_formats(db: Path) -> None:
    storage = history(db)
    run_id = stored_run(storage, NOW, [])
    code, output = invoke_main(["--db", str(db), "report", str(run_id), "--format", "none"])
    assert code == EXIT_USAGE
    assert "report needs a real format" in output
    code, output = invoke_main(["--db", str(db), "report", str(run_id), "--format", "rtf"])
    assert code == EXIT_USAGE
    assert "unknown --format" in output
    assert "rtf" in output


def test_report_junit_to_stdout_is_parseable_xml(db: Path) -> None:
    storage = history(db)
    run_id = stored_run(storage, NOW, [issue("broken"), issue("slow")])
    out = invoke(["report", str(run_id), "--format", "junit"], db=db).stdout
    root = ET.fromstring(out)
    assert root.attrib["failures"] == "1"
    assert root.attrib["skipped"] == "1"


# ------------------------------------------------------------------------------- watch
def test_watch_once_crawls_and_reports_a_baseline(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_network(monkeypatch, {f"{SITE}/": page('<a href="/gone">x</a>')})
    result = invoke(["watch", f"{SITE}/", "--once"], db=db)
    assert result.exit_code == EXIT_OK  # the first crawl is the baseline, nothing is "new"
    assert "baseline crawl" in result.stdout
    assert "every" in result.stdout
    assert len(history(db).list_runs()) == 1


def test_watch_notifies_and_exits_on_new_issues(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes: dict[str, dict[str, Any]] = {f"{SITE}/": page("<p>hi</p>")}
    stub_network(monkeypatch, routes)
    assert invoke(["watch", f"{SITE}/", "--once"], db=db).exit_code == EXIT_OK
    # a second crawl, this time with a broken link: watch must notice it
    routes[f"{SITE}/"] = page('<a href="/gone">x</a>')
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli.httpx, "post", lambda url, **kw: posts.append({"url": url, **kw}) or _DummyResponse()
    )
    result = invoke(
        ["watch", f"{SITE}/", "--once", "--notify-webhook", "https://hook.test/x"], db=db
    )
    assert result.exit_code == EXIT_ISSUES
    assert len(posts) == 1
    payload = posts[0]["json"]
    assert payload["new_issues"][0]["code"] == "broken"
    assert payload["site"] == f"{SITE}/"


class _DummyResponse:
    status_code = 200


def test_watch_swallows_webhook_failures(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes: dict[str, dict[str, Any]] = {f"{SITE}/": page("<p>hi</p>")}
    stub_network(monkeypatch, routes)
    invoke(["watch", f"{SITE}/", "--once"], db=db)
    routes[f"{SITE}/"] = page('<a href="/gone">x</a>')

    def boom(_url: str, **_kw: Any) -> None:
        raise cli.httpx.ConnectError("nope")

    monkeypatch.setattr(cli.httpx, "post", boom)
    result = invoke(
        ["watch", f"{SITE}/", "--once", "--notify-webhook", "https://hook.test/x"], db=db
    )
    assert result.exit_code == EXIT_ISSUES
    assert "webhook failed" in result.output


def test_watch_accepts_every_crawl_flag(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Config] = []

    def fake(state, console, config, seeds, storage, *, interrupt=None):  # type: ignore[no-untyped-def]
        del state, console, seeds, storage, interrupt
        seen.append(config)
        return CrawlReport(site=SITE, seeds=[f"{SITE}/"], run_id=1)

    monkeypatch.setattr(cli, "run_crawl", fake)
    result = invoke(
        [
            "watch",
            f"{SITE}/",
            "--once",
            "--every",
            "30m",
            "--max-depth",
            "2",
            "--retries",
            "0",
            "--include",
            "glob:**/docs/**",
            "--exclude",
            "re:/draft",
            "--sitemap",
            "--check-fragments",
            "--ignore-robots",
            "--include-subdomains",
            "--allow-private",
            "--no-external",
            "--ignore-status",
            "429",
            "--slow-ms",
            "250",
            "--user-agent",
            "UA/2",
            "--format",
            "json",
        ],
        db=db,
    )
    assert result.exit_code == EXIT_OK, result.stdout
    config = seen[0]
    assert config.crawl.max_depth == 2
    assert config.rules.sitemap is True
    assert config.rules.check_fragments is True
    assert config.rules.ignore_robots is True
    assert config.rules.allow_private is True
    assert config.rules.follow_external is False
    assert config.rules.ignore_status == [429]
    assert config.detection.slow_ms == 250
    assert config.output.format == "json"
    assert "robots.txt is being ignored" in result.stdout


def test_watch_output_file_per_cycle(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_network(monkeypatch, {f"{SITE}/": page('<a href="/gone">x</a>')})
    target = tmp_path / "latest.json"
    result = invoke(
        ["watch", f"{SITE}/", "--once", "--format", "json", "--output", str(target)], db=db
    )
    assert result.exit_code == EXIT_OK  # baseline run, nothing is new yet
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["issues"][0]["code"] == "broken"
    assert payload["run"]["status"] == "completed"


def test_watch_rejects_a_bogus_interval(db: Path) -> None:
    code, output = invoke_main(["--db", str(db), "watch", SITE, "--every", "whenever"])
    assert code == EXIT_USAGE
    assert "invalid duration" in output


def test_watch_without_a_seed_is_a_usage_error(workdir: Path, db: Path) -> None:
    code, output = invoke_main(["--db", str(db), "watch"])
    assert code == EXIT_USAGE
    assert "no seed URL" in output


def test_watch_interval_is_parsed(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    result = invoke(["watch", f"{SITE}/", "--every", "6h", "--once"], db=db)
    assert "every 6.00h" in result.stdout


# ------------------------------------------------------------------------------- prune
def test_prune_without_matches_says_so(db: Path) -> None:
    storage = history(db)
    stored_run(storage, dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1), [])
    result = invoke(["prune", "--older-than", "90d"], db=db)
    assert result.exit_code == EXIT_OK
    assert "nothing older than 90d" in result.stdout


def test_prune_asks_before_deleting(db: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW - dt.timedelta(days=400), [])
    result = invoke(["prune", "--older-than", "90d"], db=db, input="n\n")
    assert result.exit_code not in (EXIT_OK,)  # aborted, nothing deleted
    assert storage.get_run(1) is not None
    result = invoke(["prune", "--older-than", "90d", "--yes"], db=db)
    assert result.exit_code == EXIT_OK
    assert "deleted" in result.stdout


def test_prune_always_keeps_the_newest_run(db: Path) -> None:
    storage = history(db)
    stored_run(storage, NOW - dt.timedelta(days=400), [])
    stored_run(storage, NOW - dt.timedelta(days=399), [])
    result = invoke(["prune", "--older-than", "90d", "--yes", "--vacuum"], db=db)
    assert result.exit_code == EXIT_OK
    assert len(storage.list_runs()) == 1
    assert "1 run(s)" in invoke(["runs"], db=db).stdout


# ------------------------------------------------------------------- error handling
def test_unexpected_errors_are_friendly_unless_verbose(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_crawl", explode)
    code, output = invoke_main(["--db", str(db), "crawl", SITE])
    assert code == EXIT_USAGE
    assert "unexpected error: RuntimeError" in output
    assert "--verbose" in output
    assert "Traceback" not in output
    with pytest.raises(RuntimeError, match="kaboom"):
        cli.main(["--verbose", "--db", str(db), "crawl", SITE])


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    code, output = invoke_main(["--config", str(tmp_path / "nope.yaml"), "runs"])
    assert code == EXIT_USAGE
    assert "config file not found" in output


def test_broken_yaml_is_reported_with_a_hint(workdir: Path) -> None:
    (workdir / "linkkeep.yaml").write_text("site: [unclosed\n", encoding="utf-8")
    code, output = invoke_main(["runs"])
    assert code == EXIT_USAGE
    assert "linkkeep.yaml" in output
    assert "Traceback" not in output


def test_invalid_option_value_is_reported_by_typer(db: Path) -> None:
    result = invoke(["crawl", SITE, "--concurrency", "0"], db=db)
    assert result.exit_code == EXIT_USAGE
    assert "Invalid value for '--concurrency'" in result.output


def test_database_path_that_cannot_be_used_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    code, output = invoke_main(["--db", str(blocker / "nested.sqlite"), "runs"])
    assert code == EXIT_USAGE
    assert "cannot use the history database" in output


def test_linkkeep_home_overrides_the_default_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINKKEEP_HOME", str(tmp_path / "elsewhere"))
    config = Config.from_dict({})
    assert config.resolved_database(None) == tmp_path / "elsewhere" / "linkkeep.sqlite"


def test_config_database_is_used_when_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    target = tmp_path / "custom.sqlite"
    (workdir / "linkkeep.yaml").write_text(f"database: {target}\n", encoding="utf-8")
    result = invoke(["runs"])
    assert result.exit_code == EXIT_OK
    assert target.exists()
    assert target.name in result.stdout


def test_crawl_report_helper_functions_are_covered(db: Path) -> None:
    empty = CrawlReport(site=SITE, seeds=[f"{SITE}/"], counts=Counts())
    assert cli.exit_code_for(empty, "error") == EXIT_OK
    failed = CrawlReport(site=SITE, seeds=[], status=RunStatus.FAILED)
    assert cli.exit_code_for(failed, "never") == EXIT_CRAWL_FAILED
    assert cli.reaches(issue("broken"), "error") is True
    assert cli.reaches(issue("slow"), "error") is False
    assert cli.reaches(issue("slow"), "never") is False
    assert cli._validate_fail_on("warning") == "warning"
    assert cli._run_json(_fake_run(), history(db))["site"] == SITE


def _fake_run():  # type: ignore[no-untyped-def]
    from linkkeep.models import RunRecord

    return RunRecord(
        id=7, site=SITE, started_at=NOW, status=RunStatus.COMPLETED, counts=Counts(), seeds=[]
    )


def test_sleep_with_interrupt_returns_immediately_when_stopped() -> None:
    import asyncio

    stop = asyncio.Event()
    stop.set()
    assert cli.sleep_with_interrupt(30.0, stop) is True


def test_run_crawl_is_a_thin_wrapper(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real wiring: build a fetcher + crawler, run it, close the client."""
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>hi</p>")}, allow_unknown=True, default_status=200
    )
    config = Config.from_dict({"rules": {"allow_private": True}})
    state = cli.Globals(db=db)
    console = state.console()
    report = cli.run_crawl(state, console, config, [f"{SITE}/"], None)
    assert report.counts.pages == 1
    assert report.run_id is None  # no storage passed in


def test_watch_multiple_cycles_use_fresh_interrupt_events(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_network(
        monkeypatch, {f"{SITE}/": page("<p>ok</p>")}, allow_unknown=True, default_status=200
    )
    sleeps: list[float] = []

    def stop_after_two(seconds: float, stop: object) -> bool:
        sleeps.append(seconds)
        return len(sleeps) >= 2

    monkeypatch.setattr(cli, "sleep_with_interrupt", stop_after_two)
    result = invoke(["watch", f"{SITE}/", "--every", "1s", "-q"], db=db)
    assert result.exit_code == EXIT_OK
    records = history(db).list_runs()
    assert len(records) == 2
    assert all(record.status is RunStatus.COMPLETED for record in records)


def test_watch_unreachable_once_returns_crawl_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_network(monkeypatch, {}, default_status=None)
    result = invoke(["watch", f"{SITE}/", "--once"], db=db)
    assert result.exit_code == EXIT_CRAWL_FAILED
