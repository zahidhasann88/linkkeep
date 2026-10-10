# Changelog

All notable changes to Linkkeep are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), and commit subjects follow
[Conventional Commits](https://www.conventionalcommits.org/) (see
[CONTRIBUTING.md](CONTRIBUTING.md#commit-messages-and-pull-requests)).

This file is generated from the commit history at release time, so the entries below are
grouped the way `git log` was used, not the way the code is laid out.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-10-10

First public release. The whole feature set is described in
[the README](README.md#why); this is the
same information sorted by what it means for someone using the tool.

### Added

**Crawling**

- Crawl a site from one or more seed URLs: internal pages are crawled breadth-first,
  `a[href]`, `<link href>`, `img src`, `img srcset`, `script src`, `iframe src` and
  `source src` are all followed, `<base href>` is honoured,
  and off-site links are status-checked without ever fetching the remote page.
- Same-registrable-domain matching (`example.com` also covers `www.example.com`), with
  `--include-subdomains` for every subdomain and `--include` / `--exclude` patterns
  (regex or glob) for everything else.
- Politeness by default: `robots.txt` is fetched and cached per origin with retries
  (`--ignore-robots` prints a warning), a strict token bucket limits each host to
  `--rate` requests/second, `Crawl-delay` is respected when published, `max_pages` is
  enforced with an atomic counter, and the `User-Agent` advertises the project URL.
- Loopback, private and link-local addresses are refused unless `--allow-private` - it
  would otherwise be possible to point Linkkeep at an internal service through a link.
- `Ctrl+C` finishes the in-flight requests, marks the run `interrupted` and saves what was
  found; the next crawl's diff still sees those findings.
- `--sitemap` seeds the queue from `/sitemap.xml` and the `Sitemap:` line in robots.txt
  (one index level deep).

**Issue detection** - 11 types, each with a stable code and a severity (`issues.py`)

- `broken` (error), `unreachable` (error) with the failure kind in the reason,
  `redirect_loop` (error), `redirect_to_error` (error).
- `redirect_chain` (warning), `redirect_http_downgrade` (warning), `mixed_content`
  (warning), `missing_fragment` (warning, `--check-fragments`).
- `slow` (info), `redirect_temporary_permanent_internal` (info), `blocked_by_robots` (info).
- Every finding records the page it was found on, the element and attribute, the link text,
  the target, the final URL, the status, the full redirect chain, the reason and the time.

**Commands** (`cli.py`, exit codes `0` clean · `1` findings at or above `--fail-on` ·
`2` usage/config/IO error · `3` crawl failed)

- `linkkeep init` writes a commented `linkkeep.yaml` (`--force` to overwrite).
- `linkkeep crawl URL...` with `--max-pages`, `--max-depth`, `--concurrency`, `--rate`,
  `--timeout`, `--include`, `--exclude`, `--sitemap`, `--check-fragments`,
  `--ignore-robots`, `--ignore-status`, `--allow-private`, `--header`, `--cookie`,
  `--fail-on`, `--format`, `--output`, `--json`, `--store/--no-store`, `--no-color`,
  `--verbose`.
- `linkkeep runs` - recent runs; `--site`, `--limit`, `--json`.
- `linkkeep diff` - NEW / FIXED / PERSISTING plus "broken since" and a run count; takes
  zero, one or two run ids; `--all-pages`, `--json`, and the same report flags.
- `linkkeep report [RUN]` - re-render any stored run in any format, `--format html`
  produces a single self-contained file you can attach to a ticket.
- `linkkeep watch URL --every 6h` - crawl-and-diff forever with `--once` and
  `--notify-webhook`; a failing webhook never breaks the loop.
- `linkkeep prune --older-than 90d` (`--yes`, `--keep-last`, `--vacuum`).
- Global `--config`, `--db`, `--no-color`, `-q/--quiet`, `-v/--verbose`, `--version`.

**Storage** (`storage.py`)

- SQLite in the platform data directory (`LINKKEEP_HOME` or `--db` to move it), WAL mode,
  `runs` / `pages` / `links` / `issues` tables, `SCHEMA_VERSION` with forward migrations,
  stale `running` rows marked `interrupted` on open.
- Issue identity is a fingerprint of type + source + target, which is what makes diff and
  "broken since" stable across runs; `UNIQUE(run_id, fingerprint)` deduplicates.
- The config used for a run is stored with auth **values replaced by names** - secrets are
  never written to disk or printed.

**Reports** (`report/`)

- `text` for terminals (rich markup, but no colour under `--no-color`, in a pipe, or when
  `NO_COLOR` is set), `markdown` for GitHub (severity icons, `> [!NOTE]` callouts,
  collapsible issue lists), `html` as one self-contained file with an inline
  type/severity/URL filter that works offline, `json` with a documented versioned schema
  (`schema_version: 1`), `junit` for CI that only speaks JUnit, and `none` to skip rendering.
- `--format` accepts several values and `--output` accepts a directory, so one run can
  produce all of them.

**Project**

- `examples/linkkeep.yaml` and `examples/github-actions-linkcheck.yml` (a scheduled crawl
  with cached history that fails only on new problems).
- The fixture site ships inside the package (`python -m linkkeep.fixture_site`) so `make
  demo`, the integration tests and the README samples need no internet.
- Issue/PR templates, a `SECURITY.md`, CI across Ubuntu/macOS/Windows on Python 3.10-3.13
  with an enforced 85% coverage gate, release via PyPI trusted publishing, Dependabot for
  pip and Actions.

### Notes for the curious

- `slow` measures server time: the rate limiter's wait is subtracted, so a busy crawl does
  not report every page as slow.
- An unset `auth` `env:VAR` drops that header (no blank `Authorization`) and warns about the
  variable name.
- `ignore_status` accepts `404,429` or `400:404`; excluded statuses produce no issue anywhere,
  not even as a redirect hop.
- `redirect_temporary_permanent_internal` is the one finding that can legitimately become an
  error later: it is the "did you mean 301?" nudge, so it is `info` and never fails CI.

[unreleased]: https://github.com/YOUR-OWNER/linkkeep/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YOUR-OWNER/linkkeep/releases/tag/v0.1.0
