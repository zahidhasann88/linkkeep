"""The ``linkkeep`` command line interface.

Exit codes (documented in the README and asserted by tests):

* ``0`` - no issues at or above ``--fail-on``
* ``1`` - issues at or above ``--fail-on`` (new issues, for ``diff`` and ``watch``)
* ``2`` - usage or configuration error
* ``3`` - the crawl could not run at all (e.g. every seed is unreachable)
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from linkkeep import __version__
from linkkeep.config import (
    INIT_CONFIG_YAML,
    REPO_URL,
    Config,
    ConfigError,
    LinkkeepError,
    deep_merge,
    find_config_file,
    parse_duration,
    read_yaml_config,
)
from linkkeep.crawler import Crawler, CrawlReport, prepare_seeds
from linkkeep.diff import DiffResult, describe_diff, diff_runs, resolve_run_ids
from linkkeep.fetcher import HttpxFetcher
from linkkeep.models import Counts, Issue, RunRecord, RunStatus, Severity
from linkkeep.ratelimit import RateLimiter
from linkkeep.report import from_crawl, from_storage, render, stamp, summary_line, write_output
from linkkeep.storage import Storage

EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_USAGE = 2
EXIT_CRAWL_FAILED = 3

FORMATS = ("text", "json", "markdown", "html", "junit")

app = typer.Typer(
    name="linkkeep",
    help=f"Crawl a site, find broken links and redirect problems, keep a history.\n\nDocs: {REPO_URL}",
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)


@dataclass
class Globals:
    """State shared by every command (set by the ``--config``/``--db`` group flags)."""

    config_path: Path | None = None
    db: Path | None = None
    no_color: bool = False
    quiet: bool = False
    verbose: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def console(self, *, width: int | None = None) -> Console:
        """``width`` is only used by tables that would otherwise truncate their columns."""
        return Console(no_color=self.no_color, highlight=False, width=width)


def _state(ctx: typer.Context) -> Globals:
    state = ctx.obj if isinstance(ctx.obj, Globals) else Globals()
    if not isinstance(ctx.obj, Globals):
        ctx.obj = state
    return state


def _apply_style_flags(state: Globals, *, no_color: bool, quiet: bool, verbose: bool) -> None:
    state.no_color = state.no_color or no_color
    state.quiet = state.quiet or quiet
    state.verbose = state.verbose or verbose


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"linkkeep {__version__}")
        raise typer.Exit(EXIT_OK)


# --------------------------------------------------------------------------- app
@app.callback()
def cli(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        dir_okay=False,
        help="Config file to read (default: linkkeep.yaml in the current directory or a parent).",
    ),
    db: Path | None = typer.Option(
        None,
        "--db",
        help="SQLite history file (default: the platform data dir, or $LINKKEEP_HOME).",
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable coloured output."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print what was asked for."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Debug output and full tracebacks."
    ),
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Linkkeep keeps a history of your site's link health."""
    del version
    ctx.obj = Globals(config_path=config, db=db, no_color=no_color, quiet=quiet, verbose=verbose)


# -------------------------------------------------------------------------- init
@app.command()
def init(
    ctx: typer.Context,
    path: Path = typer.Option(
        Path("linkkeep.yaml"), "--path", "-p", help="Where to write the config file."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing file."),
) -> None:
    """Write a commented ``linkkeep.yaml`` to get you started."""
    state = _state(ctx)
    console = state.console()
    target = Path(path)
    if target.exists() and not force:
        raise LinkkeepError(f"{target} already exists (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(INIT_CONFIG_YAML, encoding="utf-8")
    console.print(f"[green]wrote[/] {target}")
    console.print(
        f"next: [bold]linkkeep crawl https://your-site.example[/] - full reference at {REPO_URL}"
    )


# ------------------------------------------------------------------------- crawl
@dataclass
class CrawlFlags:
    """Crawl options that both ``crawl`` and ``watch`` accept."""

    max_pages: int | None = None
    max_depth: int | None = None
    concurrency: int | None = None
    rate: float | None = None
    timeout: float | None = None
    retries: int | None = None
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    sitemap: bool = False
    check_fragments: bool = False
    ignore_robots: bool = False
    include_subdomains: bool = False
    allow_private: bool = False
    no_external: bool = False
    ignore_status: list[int] = field(default_factory=list)
    slow_ms: int | None = None
    user_agent: str | None = None

    def to_overrides(self) -> dict[str, Any]:
        """Nested dict of only the values the user actually passed."""
        crawl: dict[str, Any] = {}
        for name in ("max_pages", "max_depth", "concurrency", "rate", "timeout", "retries"):
            value = getattr(self, name)
            if value is not None:
                crawl[name] = value
        rules: dict[str, Any] = {}
        for name in ("include", "exclude", "ignore_status"):
            value = getattr(self, name)
            if value:
                rules[name] = list(value)
        for name in (
            "sitemap",
            "check_fragments",
            "ignore_robots",
            "include_subdomains",
            "allow_private",
        ):
            if getattr(self, name):
                rules[name] = True
        if self.no_external:
            rules["follow_external"] = False
        overrides: dict[str, Any] = {}
        if crawl:
            overrides["crawl"] = crawl
        if rules:
            overrides["rules"] = rules
        if self.slow_ms is not None:
            overrides["detection"] = {"slow_ms": self.slow_ms}
        if self.user_agent:
            overrides["user_agent"] = self.user_agent
        return overrides


# fmt: off
@app.command()
def crawl(
    ctx: typer.Context,
    url: list[str] = typer.Argument(None, help="Seed URL(s). Defaults to `site:` from the config file."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Maximum internal pages to fetch."),
    max_depth: int | None = typer.Option(None, "--max-depth", min=0, help="Maximum link depth (default: unlimited)."),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1, max=200, help="Simultaneous requests."),
    rate: float | None = typer.Option(None, "--rate", min=0.05, help="Requests per second, per host."),
    timeout: float | None = typer.Option(None, "--timeout", min=0.5, help="Request timeout in seconds."),
    retries: int | None = typer.Option(None, "--retries", min=0, max=5, help="Retries for transient errors."),
    include: list[str] = typer.Option([], "--include", help="Only crawl/check URLs matching this regex or glob (repeatable)."),
    exclude: list[str] = typer.Option([], "--exclude", help="Skip URLs matching this regex or glob (repeatable)."),
    sitemap: bool = typer.Option(False, "--sitemap", help="Also queue URLs listed in sitemap.xml."),
    check_fragments: bool = typer.Option(False, "--check-fragments", help="Verify #anchors exist on the target page."),
    ignore_robots: bool = typer.Option(False, "--ignore-robots", help="Ignore robots.txt (not recommended)."),
    include_subdomains: bool = typer.Option(False, "--include-subdomains", help="Treat subdomains of the seed host as internal."),
    allow_private: bool = typer.Option(False, "--allow-private", help="Allow loopback/private hosts (local dev, CI)."),
    no_external: bool = typer.Option(False, "--no-external", help="Do not check links pointing off-site."),
    ignore_status: list[int] = typer.Option([], "--ignore-status", help="Never report these status codes, e.g. 999 (LinkedIn)."),
    slow_ms: int | None = typer.Option(None, "--slow-ms", min=1, help="Threshold for `slow` info issues."),
    user_agent: str | None = typer.Option(None, "--user-agent", help="Override the User-Agent header."),
    fail_on: str | None = typer.Option(None, "--fail-on", help="error|warning|info|never (default: error)."),
    output_format: str | None = typer.Option(None, "--format", "-f", help=f"{'|'.join(FORMATS)} (default: text)."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the report to FILE instead of stdout."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable coloured output."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the summary."),
    verbose: bool = typer.Option(False, "--verbose", help="Show progress and full tracebacks."),
    store: bool = typer.Option(True, "--store/--no-store", help="Record this run in the SQLite history."),
) -> None:
    """Crawl a site and report broken links, redirect problems and unreachable pages."""
# fmt: on
    state = _state(ctx)
    _apply_style_flags(state, no_color=no_color, quiet=quiet, verbose=verbose)
    console = state.console()
    flags = CrawlFlags(
        max_pages=max_pages, max_depth=max_depth, concurrency=concurrency, rate=rate, timeout=timeout,
        retries=retries, include=list(include), exclude=list(exclude), sitemap=sitemap,
        check_fragments=check_fragments, ignore_robots=ignore_robots, include_subdomains=include_subdomains,
        allow_private=allow_private, no_external=no_external, ignore_status=list(ignore_status),
        slow_ms=slow_ms, user_agent=user_agent,
    )
    overrides = flags.to_overrides()
    if fail_on is not None:
        overrides.setdefault("output", {})["fail_on"] = _validate_fail_on(fail_on)
    if output_format is not None:
        _validate_format(output_format)
        overrides.setdefault("output", {})["format"] = output_format
    config = load_config(state, overrides)
    console = state.console()
    if config.rules.ignore_robots:
        console.print("[yellow]warning:[/] robots.txt is being ignored (--ignore-robots). Be kind to this host.")
    for note in config.unresolved_auth():
        console.print(f"[yellow]warning:[/] {note} - requests will be sent without it")
    seeds = list(url or []) or config.seed_urls()
    if not seeds:
        raise ConfigError(
            "no seed URL given: run `linkkeep crawl https://example.com` or set `site:` in linkkeep.yaml"
        )
    storage = open_storage(state, config) if store else None
    if storage is not None:
        storage.mark_stale_runs_interrupted()
    report = run_crawl(state, console, config, seeds, storage)
    render_and_emit(state, console, config, report, fmt=config.output.format, output=output)
    raise typer.Exit(code=exit_code_for(report, config.output.fail_on))


# --------------------------------------------------------------------------- runs
@app.command()
def runs(
    ctx: typer.Context,
    site: str | None = typer.Option(None, "--site", help="Only show runs for this site."),
    limit: int = typer.Option(20, "--limit", min=1, max=1000, help="How many runs to show."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List past crawls."""
    state = _state(ctx)
    config = load_config(state, {})
    storage = open_storage(state, config)
    records = storage.list_runs(site=site, limit=limit)
    # 9 columns need more than rich's default 80 characters, or the site gets cut off
    widest = max((len(run.site) for run in records), default=0)
    console = state.console(width=max(88, widest + 80))
    if as_json:
        typer.echo(json.dumps([_run_json(run, storage) for run in records], indent=2))
        raise typer.Exit(EXIT_OK)
    if not records:
        console.print(
            f"no runs recorded yet in [bold]{storage.path}[/]\n"
            "run [bold]linkkeep crawl https://example.com[/] first."
        )
        raise typer.Exit(EXIT_OK)
    table = Table(box=None, pad_edge=False, header_style="bold dim", show_edge=False)
    for name, justify in (
        ("run", "right"), ("site", "left"), ("started", "left"), ("status", "left"),
        ("pages", "right"), ("checked", "right"), ("errors", "right"), ("warn", "right"), ("info", "right"),
    ):
        table.add_column(name, justify=justify, overflow="fold")  # type: ignore[arg-type]
    for run in records:
        counts: Counts = run.counts
        style = {"completed": "green", "interrupted": "yellow", "failed": "red"}.get(run.status.value, "white")
        table.add_row(
            str(run.id),
            run.site,
            stamp(run.started_at),
            Text(run.status.value, style=style),
            str(counts.pages),
            str(counts.checked),
            Text(str(counts.errors), style="red" if counts.errors else "dim"),
            Text(str(counts.warnings), style="yellow" if counts.warnings else "dim"),
            Text(str(counts.infos), style="cyan" if counts.infos else "dim"),
        )
    console.print(table)
    console.print(f"[dim]{len(records)} run(s) in {storage.path}[/]")


# --------------------------------------------------------------------------- diff
@app.command()
def diff(
    ctx: typer.Context,
    run_a: int | None = typer.Argument(None, help="Older run id (default: the run before RUN_B)."),
    run_b: int | None = typer.Argument(None, help="Newer run id (default: the most recent run)."),
    site: str | None = typer.Option(None, "--site", help="Restrict the automatic run choice to this site."),
    output_format: str | None = typer.Option(None, "--format", "-f", help=f"{'|'.join(FORMATS)} (default: text)."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the report to FILE instead of stdout."),
    fail_on: str | None = typer.Option(None, "--fail-on", help="Exit 1 when NEW issues reach this level."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable coloured output."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the one-line summary."),
    verbose: bool = typer.Option(False, "--verbose", help="Show full tracebacks."),
) -> None:
    """Compare two crawls: what is NEW, what is FIXED, what is PERSISTING."""
    state = _state(ctx)
    _apply_style_flags(state, no_color=no_color, quiet=quiet, verbose=verbose)
    console = state.console()
    overrides: dict[str, Any] = {}
    if fail_on is not None:
        overrides["output"] = {"fail_on": _validate_fail_on(fail_on)}
    config = load_config(state, overrides)
    console = state.console()
    storage = open_storage(state, config)
    try:
        first, second = resolve_run_ids(storage, run_a, run_b, site=site)
        result = diff_runs(storage, first, second)
    except LinkkeepError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(EXIT_USAGE) from exc
    data = from_storage(storage, second, diff=result)
    fmt = output_format or config.output.format
    _validate_format(fmt)
    if fmt != "none":
        rendered = render(data, fmt)
        to_stdout = output is None or str(output) == "-"
        if to_stdout:
            if fmt == "text":
                if state.quiet:
                    console.print(describe_diff(result))
                else:
                    console.print(rendered, markup=False, highlight=False)
            else:
                typer.echo(rendered, nl=False)
        else:
            write_output(rendered, output, console_print=None)
            console.print(f"[green]wrote[/] {output}")
    failing = [issue for issue in result.new if reaches(issue, config.output.fail_on)]
    if state.verbose:
        console.print(f"[dim]compared run #{first} with run #{second}[/]")
    raise typer.Exit(EXIT_ISSUES if failing else EXIT_OK)


# ------------------------------------------------------------------------ report
@app.command()
def report(
    ctx: typer.Context,
    run_id: int = typer.Argument(..., help="Run id, from `linkkeep runs`."),
    output_format: str = typer.Option("markdown", "--format", "-f", help=f"{'|'.join(FORMATS)}."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write to FILE instead of stdout."),
    diff_with_previous: bool = typer.Option(
        False, "--diff-with-previous", help="Also include the NEW/FIXED/PERSISTING section for the previous run."
    ),
) -> None:
    """Export a stored run as text, markdown, HTML, JSON or JUnit."""
    state = _state(ctx)
    console = state.console()
    config = load_config(state, {})
    storage = open_storage(state, config)
    _validate_format(output_format)
    if output_format == "none":
        raise ConfigError("report needs a real format: " + "|".join(FORMATS))
    run = storage.get_run(run_id)
    if run is None:
        raise LinkkeepError(f"no run with id {run_id} in {storage.path}")
    result: DiffResult | None = None
    if diff_with_previous:
        older = [item for item in storage.list_runs(site=run.site, limit=1000) if item.id < run_id]
        if older:
            result = diff_runs(storage, older[0].id, run_id)
        else:
            console.print("[yellow]no earlier run for this site, so the diff section is empty[/]")
    data = from_storage(storage, run_id, diff=result)
    rendered = render(data, output_format)
    if output is None or str(output) == "-":
        if output_format == "text":
            console.print(rendered, markup=False, highlight=False)
        else:
            typer.echo(rendered, nl=False)
    else:
        write_output(rendered, output, console_print=None)
        console.print(f"[green]wrote[/] {output}")
    raise typer.Exit(EXIT_OK)


# ------------------------------------------------------------------------- watch
# fmt: off
@app.command()
def watch(
    ctx: typer.Context,
    url: list[str] = typer.Argument(None, help="Seed URL(s)."),
    every: str | None = typer.Option(None, "--every", help="Interval between crawls, e.g. 30m, 6h, 1d."),
    notify_webhook: str | None = typer.Option(None, "--notify-webhook", help="POST a JSON summary when NEW issues appear."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Maximum internal pages per crawl."),
    max_depth: int | None = typer.Option(None, "--max-depth", min=0, help="Maximum link depth (default: unlimited)."),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1, max=200, help="Simultaneous requests."),
    rate: float | None = typer.Option(None, "--rate", min=0.05, help="Requests per second, per host."),
    timeout: float | None = typer.Option(None, "--timeout", min=0.5, help="Request timeout in seconds."),
    retries: int | None = typer.Option(None, "--retries", min=0, max=5, help="Retries for transient errors."),
    include: list[str] = typer.Option([], "--include", help="Only crawl/check URLs matching this regex or glob (repeatable)."),
    exclude: list[str] = typer.Option([], "--exclude", help="Skip URLs matching this regex or glob (repeatable)."),
    sitemap: bool = typer.Option(False, "--sitemap", help="Also queue URLs listed in sitemap.xml."),
    check_fragments: bool = typer.Option(False, "--check-fragments", help="Verify #anchors exist on the target page."),
    ignore_robots: bool = typer.Option(False, "--ignore-robots", help="Ignore robots.txt (not recommended)."),
    include_subdomains: bool = typer.Option(False, "--include-subdomains", help="Treat subdomains of the seed host as internal."),
    allow_private: bool = typer.Option(False, "--allow-private", help="Allow loopback/private hosts (local dev, CI)."),
    no_external: bool = typer.Option(False, "--no-external", help="Do not check links pointing off-site."),
    ignore_status: list[int] = typer.Option([], "--ignore-status", help="Never report these status codes (repeatable)."),
    slow_ms: int | None = typer.Option(None, "--slow-ms", min=1, help="Threshold for `slow` info issues."),
    user_agent: str | None = typer.Option(None, "--user-agent", help="Override the User-Agent header."),
    output_format: str | None = typer.Option(None, "--format", "-f", help=f"{'|'.join(FORMATS)} for each cycle's report (default: text)."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write each cycle's report to FILE (overwritten)."),
    fail_on: str | None = typer.Option(None, "--fail-on", help="Exit 1 when NEW issues reach this level."),
    once: bool = typer.Option(False, "--once", help="Run a single iteration and exit (handy in CI)."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable coloured output."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the one-line diff summary."),
    verbose: bool = typer.Option(False, "--verbose", help="Show per-page progress."),
) -> None:
    """Crawl on an interval and print the diff each time. cron or CI is still the recommended scheduler."""
# fmt: on
    state = _state(ctx)
    _apply_style_flags(state, no_color=no_color, quiet=quiet, verbose=verbose)
    console = state.console()
    flags = CrawlFlags(
        max_pages=max_pages, max_depth=max_depth, concurrency=concurrency, rate=rate, timeout=timeout,
        retries=retries, include=list(include), exclude=list(exclude), sitemap=sitemap,
        check_fragments=check_fragments, ignore_robots=ignore_robots, include_subdomains=include_subdomains,
        allow_private=allow_private, no_external=no_external, ignore_status=list(ignore_status),
        slow_ms=slow_ms, user_agent=user_agent,
    )
    overrides = flags.to_overrides()
    if output_format is not None:
        _validate_format(output_format)
        overrides.setdefault("output", {})["format"] = output_format
    if fail_on is not None:
        overrides.setdefault("output", {})["fail_on"] = _validate_fail_on(fail_on)
    config = load_config(state, overrides)
    console = state.console()
    if config.rules.ignore_robots:
        console.print("[yellow]warning:[/] robots.txt is being ignored (--ignore-robots). Be kind to this host.")
    seeds = list(url or []) or config.seed_urls()
    if not seeds:
        raise ConfigError("no seed URL given: `linkkeep watch https://example.com` or set `site:` in linkkeep.yaml")
    for note in config.unresolved_auth():
        console.print(f"[yellow]warning:[/] {note} - requests will be sent without it")
    interval = parse_duration(every or config.watch.every)
    notify_webhook = notify_webhook or config.watch.notify_webhook
    storage = open_storage(state, config)
    storage.mark_stale_runs_interrupted()
    threshold = config.output.fail_on
    worst = EXIT_OK
    stop = asyncio.Event()
    install_signal_handlers(stop, console)
    console.print(
        f"[bold]watching[/] {', '.join(seeds)} every {interval / 3600:.2f}h - Ctrl+C to stop. "
        "For long-lived scheduling prefer cron or a CI schedule."
    )
    while True:
        if stop.is_set():
            break
        stop = asyncio.Event()
        install_signal_handlers(stop, console)
        started = time.time()
        report_ = run_crawl(state, console, config, seeds, storage, interrupt=stop)
        if report_.run_id is None:  # pragma: no cover - watch always stores
            raise LinkkeepError("watch needs the history database")
        if report_.status is RunStatus.FAILED:
            console.print("[red]crawl could not run; will retry at the next interval[/]")
        result: DiffResult | None = None
        # Always compare with the run before this one in the history, so `watch --once`
        # (the CI-friendly mode) produces a real diff even though it only crawls once.
        earlier = [run.id for run in storage.list_runs(site=report_.site, limit=1000) if run.id != report_.run_id]
        if earlier:
            try:
                result = diff_runs(storage, max(earlier), report_.run_id)
            except LinkkeepError as exc:  # pragma: no cover - defensive
                console.print(f"[yellow]{exc}[/]")
        # the report file is rewritten every cycle, whether or not anything changed
        fmt = output_format or config.output.format
        if output is not None and str(output) != "-":
            write_output(render(from_storage(storage, report_.run_id, diff=result), fmt), output, console_print=None)
        if result is None:
            console.print(f"[dim]run #{report_.run_id}: baseline crawl, {len(report_.issues)} issue(s) recorded[/]")
        elif state.quiet:
            console.print(describe_diff(result))
        else:
            data = from_storage(storage, report_.run_id, diff=result)
            rendered = render(data, fmt)
            if fmt == "text":
                console.print(rendered, markup=False, highlight=False)
            elif fmt != "none":
                typer.echo(rendered, nl=False)
        if result is not None:
            if notify_webhook and result.new:
                post_webhook(notify_webhook, result, report_, config)
            if any(reaches(issue, threshold) for issue in result.new):
                worst = EXIT_ISSUES
        if report_.status is RunStatus.FAILED:
            worst = EXIT_CRAWL_FAILED
        if once:
            break
        if sleep_with_interrupt(max(1.0, interval - (time.time() - started)), stop):
            break
    raise typer.Exit(code=worst)


# ------------------------------------------------------------------------- prune
@app.command()
def prune(
    ctx: typer.Context,
    older_than: str = typer.Option("90d", "--older-than", help="Delete runs started before this long ago, e.g. 90d."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    vacuum: bool = typer.Option(False, "--vacuum", help="Reclaim space in the SQLite file afterwards."),
) -> None:
    """Delete old runs from the history database (the newest run per site is always kept)."""
    state = _state(ctx)
    console = state.console()
    config = load_config(state, {})
    storage = open_storage(state, config)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=parse_duration(older_than))
    matching = [run for run in storage.list_runs(limit=100000) if run.started_at < cutoff]
    if not matching:
        console.print(f"nothing older than {older_than} to remove ({storage.path})")
        raise typer.Exit(EXIT_OK)
    console.print(
        f"{len(matching)} run(s) older than {older_than} would be deleted "
        f"(ids {min(run.id for run in matching)}-{max(run.id for run in matching)}); "
        "the newest run per site is always kept."
    )
    if not yes:
        typer.confirm("delete them?", abort=True, default=False)
    deleted = storage.prune(older_than=cutoff)
    if vacuum:
        storage.vacuum()
    console.print(f"[green]deleted[/] {len(deleted)} run(s) from {storage.path}")
    raise typer.Exit(EXIT_OK)


# ------------------------------------------------------------------- plumbing
def load_config(state: Globals, overrides: dict[str, Any]) -> Config:
    """defaults < ``linkkeep.yaml`` < CLI flags."""
    path = find_config_file(explicit=state.config_path)
    data: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        data = read_yaml_config(path)
    config = Config.from_dict(deep_merge(data, overrides))
    state.no_color = state.no_color or not config.output.color
    state.quiet = state.quiet or config.output.quiet
    state.verbose = state.verbose or config.output.verbose
    if state.db is not None:
        config = config.with_overrides({"database": str(state.db)})
    return config


_OPEN_STORAGE: list[Storage] = []


def open_storage(state: Globals, config: Config) -> Storage:
    """Open the history database, remembering it so :func:`main` can close it cleanly."""
    path = config.resolved_database(state.db)
    try:
        storage = Storage(path)
    except (sqlite3.Error, OSError) as exc:
        msg = f"cannot use the history database at {path}: {exc}"
        raise ConfigError(msg) from exc
    _OPEN_STORAGE.append(storage)
    return storage


def run_crawl(
    state: Globals,
    console: Console,
    config: Config,
    seeds: list[str],
    storage: Storage | None,
    *,
    interrupt: asyncio.Event | None = None,
) -> CrawlReport:
    """Build the fetcher + crawler and run one crawl to completion."""
    stop = interrupt if interrupt is not None else asyncio.Event()
    if interrupt is None:
        install_signal_handlers(stop, console)
    limiter = RateLimiter(config.crawl.rate)
    fetcher = HttpxFetcher(
        timeout=config.crawl.timeout,
        rate=config.crawl.rate,
        retries=config.crawl.retries,
        max_hops=config.crawl.max_redirect_hops,
        max_bytes=config.crawl.max_response_size,
        user_agent=config.user_agent,
        headers=config.safe_headers(),
        cookies=config.safe_cookies(),
        limiter=limiter,
        allow_private=config.rules.allow_private,
        auth_origins=prepare_seeds(seeds),
    )
    crawler = Crawler(
        config,
        fetcher,
        storage=storage,
        interrupt=stop,
        log=(lambda message: console.print(f"[dim]{message}[/]")) if state.verbose else None,
    )
    async def execute() -> CrawlReport:
        async with fetcher:
            return await crawler.run(seeds)

    report = asyncio.run(execute())
    if state.verbose:
        console.print(f"[dim]waited {limiter.total_wait_seconds:.1f}s for rate limits ({limiter.wait_count} sleeps)[/]")
    return report


def _aclose(fetcher: HttpxFetcher) -> None:
    with contextlib.suppress(Exception):
        asyncio.run(fetcher.close())


def install_signal_handlers(stop: asyncio.Event, console: Console) -> None:
    """First Ctrl+C stops cleanly and saves; a second one gives up on in-flight requests."""

    def handler(signum: int, frame: Any) -> None:  # pragma: no cover - needs a real terminal
        del signum, frame
        if stop.is_set():
            console.print("[red]second interrupt: exiting now (the last flush is still saved)[/]")
            raise KeyboardInterrupt
        stop.set()
        console.print(
            "\n[yellow]interrupted:[/] finishing the current requests, then saving partial results "
            "(Ctrl+C again to force)"
        )

    with contextlib.suppress(ValueError, RuntimeError, AttributeError):
        signal.signal(signal.SIGINT, handler)


def sleep_with_interrupt(seconds: float, stop: asyncio.Event) -> bool:
    """Sleep in short slices so Ctrl+C stays responsive. True when interrupted."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop.is_set():
            return True
        time.sleep(min(0.5, max(0.0, deadline - time.time())))
    return stop.is_set()


def render_and_emit(
    state: Globals,
    console: Console,
    config: Config,
    report_: CrawlReport,
    *,
    fmt: str,
    output: Path | None,
) -> None:
    """Print or write the report in the requested format."""
    if fmt == "none":  # `--format none` = crawl quietly, the exit code is the output
        return
    data = from_crawl(report_)
    rendered = render(data, fmt)
    to_stdout = output is None or str(output) == "-"
    if fmt == "text" and to_stdout and state.quiet:
        console.print(summary_line(data.counts_by_severity, fail_on=config.output.fail_on))
    elif fmt == "text" and to_stdout:
        print_summary(console, report_, config)
        console.print(rendered, markup=False, highlight=False)
    elif to_stdout:
        typer.echo(rendered, nl=False)
    else:
        write_output(rendered, output, console_print=None)
        console.print(f"[green]wrote[/] {output}")


def print_summary(console: Console, report_: CrawlReport, config: Config) -> None:
    """The coloured panel printed above a text report in a terminal."""
    counts = report_.counts
    by_severity = report_.issues_by_severity
    colour = {Severity.ERROR: "red", Severity.WARNING: "yellow", Severity.INFO: "cyan"}
    words = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "info"}

    def label(severity: Severity) -> str:
        count = by_severity[severity]
        if count == 1 or severity is Severity.INFO:
            return f"{count} {words[severity]}"
        return f"{count} {words[severity]}s"

    totals = Text.assemble(
        *[
            item
            for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO)
            for item in (Text(label(severity), style=colour[severity]), Text(", "))
        ][:-1]
    )
    detail = Text(
        f"{report_.site}  {counts.pages} pages, {counts.checked}/{counts.links} links checked, "
        f"{counts.skipped_scheme} non-http skipped, {report_.duration_seconds:.1f}s"
        + ("  robots.txt ignored" if config.rules.ignore_robots else ""),
        style="dim",
    )
    style = {"completed": "green", "interrupted": "yellow", "failed": "red"}.get(report_.status.value, "white")
    run_label = f"run #{report_.run_id}" if report_.run_id is not None else "not stored"
    head = Text.assemble(Text(run_label, style="bold"), Text(" "), Text(report_.status.value, style=style))
    console.print(
        Panel(Text.assemble(totals, Text("\n"), detail), title=head, border_style=style, expand=False)
    )


def post_webhook(url: str, result: DiffResult, report_: CrawlReport, config: Config) -> None:
    """Best-effort notification. Failures never break the watch loop."""
    payload = {
        "event": "linkkeep.new_issues",
        "site": result.site,
        "run_id": report_.run_id,
        "previous_run_id": result.run_a.id,
        "summary": describe_diff(result),
        "new_issues": [issue.public_dict() for issue in result.new],
        "counts": report_.counts.model_dump(mode="json"),
        "fail_on": config.output.fail_on,
        "report_url": None,
    }
    try:
        httpx.post(url, json=payload, timeout=10.0)
    except httpx.HTTPError as exc:
        typer.echo(f"warning: webhook failed: {type(exc).__name__}", err=True)


def exit_code_for(report: CrawlReport, fail_on: str) -> int:
    """3 when nothing could be crawled, 1 when issues reach ``fail_on``, otherwise 0."""
    if report.status is RunStatus.FAILED:
        return EXIT_CRAWL_FAILED
    if any(reaches(issue, fail_on) for issue in report.issues):
        return EXIT_ISSUES
    return EXIT_OK


def reaches(issue: Issue, fail_on: str) -> bool:
    from linkkeep.issues import severity_at_least

    return severity_at_least(issue, fail_on)


def _validate_format(fmt: str) -> None:
    if fmt not in FORMATS and fmt != "none":
        msg = f"unknown --format {fmt!r} (expected one of: {', '.join(FORMATS)})"
        raise ConfigError(msg)


def _validate_fail_on(value: str) -> str:
    allowed = ("error", "warning", "info", "never")
    if value not in allowed:
        msg = f"unknown --fail-on {value!r} (expected one of: {', '.join(allowed)})"
        raise ConfigError(msg)
    return value


def _run_json(run: RunRecord, storage: Storage) -> dict[str, Any]:
    return {
        "id": run.id,
        "site": run.site,
        "status": run.status.value,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "counts": run.counts.model_dump(mode="json"),
        "notes": run.notes,
        "db": str(storage.path),
    }


def close_open_storages() -> None:
    for storage in _OPEN_STORAGE:
        with contextlib.suppress(Exception):
            storage.close()
    _OPEN_STORAGE.clear()


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point: friendly errors, no traceback unless ``--verbose``."""
    argv_all = list(sys.argv[1:] if argv is None else argv)
    chatty = "--verbose" in argv_all or "-v" in argv_all
    try:
        app(args=argv, prog_name="linkkeep", standalone_mode=True)
    except LinkkeepError as exc:
        Console(stderr=True, highlight=False).print(f"[red]error:[/] {exc}")
        sys.exit(EXIT_USAGE)
    except typer.Exit as exc:  # pragma: no cover - click handles this itself
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        Console().print("\n[yellow]interrupted[/]")
        sys.exit(EXIT_USAGE)
    except Exception as exc:
        if chatty:
            raise
        console = Console(stderr=True)
        console.print(f"[red]unexpected error:[/] {type(exc).__name__}")
        console.print(f"re-run with --verbose for the traceback, then please report it at {REPO_URL}/issues")
        sys.exit(EXIT_USAGE)
    finally:
        close_open_storages()


if __name__ == "__main__":  # pragma: no cover
    main()
