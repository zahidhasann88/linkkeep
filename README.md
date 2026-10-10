# Linkkeep

**Link health, remembered.** Linkkeep crawls a site, finds broken links, redirect
problems and unreachable resources, and keeps a history so you can see what just broke —
not just what is broken.

```console
$ linkkeep crawl https://example.com
╭──────────────────────────────── run #7 completed ─────────────────────────────────╮
│ 3 errors, 1 warning, 2 info                                                       │
│ https://example.com/  214 pages, 903/911 links checked, 27 non-http skipped, 41s │
╰───────────────────────────────────────────────────────────────────────────────────╯
```

## Why

Every site rots slowly. A page moves, an asset is renamed, a partner's host goes away, a
marketing CMS emits `/logout?next=…` links that 405 on GET. Nothing tells you, because
nobody clicks every link on a site by hand.

Existing link checkers cover part of this, and are great, but in practice you want:

* **history** — "this link has been broken for 3 weeks", not a one-off snapshot;
* **diffs** — a CI job that fails only when *you* broke something new;
* **redirect intelligence** — loops, chains that never end, `https` pages that redirect
  down to `http`, temporary redirects that should be permanent;
* **politeness by default** — robots.txt, per-host rate limits, an honest User-Agent;
* **no accounts, no SaaS** — one binary, one SQLite file, runs in a minute in CI.

Linkkeep is that tool: a single dependency-light CLI you can point at a staging site, run
weekly in GitHub Actions, or call from Python.

## Install

```bash
pipx install linkkeep          # recommended: isolated CLI on PATH
uv tool install linkkeep       # same idea, with uv
pip install linkkeep           # into the current environment
```

From a checkout (hacking on it):

```bash
git clone https://github.com/YOUR-OWNER/linkkeep && cd linkkeep
make install     # creates .venv, installs the package + dev tools, installs pre-commit
make test        # the whole suite, no network access required
make demo        # crawls the bundled demo site with the real CLI
```

Runtime dependencies: `typer`, `rich`, `httpx`, `selectolax`, `pydantic`, `pyyaml`,
`protego`. Storage uses the standard library's `sqlite3`. Python 3.10–3.13, Linux,
macOS and Windows.

## Quickstart

```bash
linkkeep init                       # writes a commented linkkeep.yaml
linkkeep crawl https://example.com  # crawl, report, and record the run
linkkeep runs                       # history of past crawls
linkkeep diff                       # what is NEW / FIXED / PERSISTING vs the last run
linkkeep report 7 --format html -o site-links.html   # export any stored run
linkkeep watch https://example.com --every 6h        # loop + webhook on new problems
linkkeep prune --older-than 90d     # tidy up the history
```

Exit codes are CI-friendly: `0` clean, `1` issues at or above `--fail-on` (default
`error`), `2` usage/configuration error, `3` the crawl could not run at all.

### A real run

Here is actual output from `make demo`, which serves a deliberately messy site on
`127.0.0.1:8077` and runs the real CLI against it (no network needed):

```console
$ linkkeep crawl http://127.0.0.1:8077/ --allow-private --check-fragments --sitemap --slow-ms 300
╭──────────────────────────────── run #1 completed ─────────────────────────────────╮
│ 16 errors, 1 warning, 3 info                                                      │
│ http://127.0.0.1:8077/  32 pages, 107/108 links checked, 4 non-http skipped, 8.4s │
╰───────────────────────────────────────────────────────────────────────────────────╯
==============================================================================
Linkkeep 0.1.0 - run #1 - http://127.0.0.1:8077/
==============================================================================
status     completed   finished 2026-10-10 09:12
notes      sitemap added 5 URL(s)

16 errors, 1 warning, 3 info
pages      32 crawled (1 blocked by robots)
links      108 found, 107 checked, 4 non-http skipped
by type    blocked_by_robots=1, broken=13, missing_fragment=1, redirect_loop=2,
redirect_temporary_permanent_internal=1, slow=1, unreachable=1
duration   8.4s

ISSUES
------------------------------------------------------------------------------

[ERROR] broken - 13  (Target responded with a 4xx/5xx status)
  from: http://127.0.0.1:8077/
    http://127.0.0.1:8077/error
      HTTP 500 the page itself
    http://127.0.0.1:8077/gone
      HTTP 410 the page itself
    http://127.0.0.1:8077/img/wide.png
      HTTP 404 the page itself
    http://localhost:8077/ext-broken
      HTTP 404 <a href> "Partner docs"
  from: http://127.0.0.1:8077/about
    http://127.0.0.1:8077/new-and-broken?utm_source=site
      HTTP 404 the page itself
  from: (seed URL, no source page)
    http://127.0.0.1:8077/missing
      HTTP 404 the page itself

[ERROR] redirect_loop - 2  (Redirect chain loops back to a URL already seen)
  from: http://127.0.0.1:8077/
    http://127.0.0.1:8077/loop-a
      HTTP 301 <a href> "Forum"
      chain: 301 http://127.0.0.1:8077/loop-a -> 301 http://127.0.0.1:8077/loop-b -> 301 http://127.0.0.1:8077/loop-a
...
```

`mailto:`, `tel:` and `javascript:` links are never fetched, but they are counted in the
summary (`4 non-http skipped`) so you can see what was ignored. `--allow-private` was
needed only because the demo site lives on loopback; see
[Refusing to poke your internal network](#refusing-to-poke-your-internal-network).

### History and diffs

```console
$ linkkeep runs
run  site                    started           status     pages  checked  errors  warn  info
  1  http://127.0.0.1:8077/  2026-10-10 09:12  completed     32      107      16     1     3
  2  http://127.0.0.1:8077/  2026-10-10 09:12  completed     32      107      15     1     3
  3  http://127.0.0.1:8077/  2026-10-10 09:12  completed     31      111       6     0     2
3 run(s) in ./demo-output/demo.sqlite
```

Run 3 is the same site after someone fixed things. `linkkeep diff` compares two runs and
groups every finding as `NEW`, `FIXED` or `PERSISTING`:

```console
$ linkkeep diff -q
http://127.0.0.1:8077/: 0 new, 11 fixed, 8 persisting

$ linkkeep diff
...
DIFF  run #3 vs run #2  (http://127.0.0.1:8077/)
      older crawl 2026-10-10 09:12, newer crawl 2026-10-10 09:12
==============================================================================
NEW (0) - problems that were not there last time: none
FIXED (11) - problems that are gone now
  [error] broken: http://127.0.0.1:8077/missing (HTTP 404)
  [error] broken: http://127.0.0.1:8077/error (HTTP 500)
      from http://127.0.0.1:8077/
  [warning] missing_fragment: http://127.0.0.1:8077/blog/post-1#nope (no id or name 'nope' on the
target page)
      from http://127.0.0.1:8077/
PERSISTING (8) - still broken
  [error] unreachable: https://partner.invalid/docs (DNS failure: ConnectError: [Errno -2] Name or
service not known)
      from http://127.0.0.1:8077/
      broken since 2026-10-10
  [error] redirect_loop: http://127.0.0.1:8077/redirect/deep1 (HTTP 301)
      from http://127.0.0.1:8077/
      broken since 2026-10-10
[exit code: 0]
```

Zero new problems, so the exit code is `0`. A *new* error would make it `1` and fail CI.
Identity across runs is a stable fingerprint (`type + source page + target URL`), so a
link that changes from 404 to 500 is reported as PERSISTING, not as two separate
problems — and `broken since` tells you how long you have been ignoring it.

### Markdown for a pull request

`linkkeep report <run> --format markdown` produces GitHub-flavoured markdown with
severity tables, `> [!WARNING]` callouts and `<details>` blocks for the long tail — paste
it into a PR comment, or write it to a file with `-o`. `--format html` writes **one
self-contained file** (inline CSS, inline JS filter box, responsive) that you can attach
to a build artifact and open offline. `--format junit` produces JUnit XML for CI reporters
and `--format json` a machine-readable document (see [JSON schema](#json-report-schema)).

## Commands

| command | what it does |
| :-- | :-- |
| `linkkeep init` | write a commented `linkkeep.yaml` (`--path`, `--force`) |
| `linkkeep crawl [URL]…` | crawl, report, and store the run |
| `linkkeep runs` | list stored runs (`--site`, `--limit`, `--json`) |
| `linkkeep diff [A] [B]` | NEW / FIXED / PERSISTING between two runs (default: the last two) |
| `linkkeep report RUN` | re-render any stored run in any format |
| `linkkeep watch URL --every 6h` | crawl on an interval, print the diff, POST a webhook on new issues (`--once` for CI) |
| `linkkeep prune --older-than 90d` | delete old runs (`--yes`, `--vacuum`) |

Global flags: `--config/-c FILE`, `--db FILE`, `--no-color`, `--quiet/-q`, `--verbose/-v`,
`--version`. Everything can also live in `linkkeep.yaml`, so CI invocations stay short.

`crawl` accepts:

```
--max-pages N          internal pages to fetch (default 1000)
--max-depth N          link depth limit (default: unlimited; 0 = seeds only)
--concurrency N        simultaneous requests (default 10)
--rate R               requests per second, per host (default 5)
--timeout S            seconds per request (default 15)
--retries N            retries for timeouts / 5xx / 429 (default 1)
--include PAT          only follow/check URLs matching this regex or glob (repeatable)
--exclude PAT          skip URLs matching this regex or glob (repeatable)
--sitemap              also queue URLs from sitemap.xml (and the robots.txt Sitemap: hint)
--check-fragments      verify that #anchors exist on the target page
--ignore-robots        crawl against robots.txt (prints a warning; don't)
--include-subdomains   treat subdomains of the seed host as internal
--allow-private        allow loopback / private addresses (local dev, CI preview)
--no-external          do not status-check links that point off-site
--ignore-status CODE   never report these status codes (repeatable, e.g. 999)
--slow-ms N            threshold for `slow` info issues (default 5000)
--user-agent STRING    override the User-Agent
--fail-on LEVEL        error|warning|info|never (default error)
--format, -f FMT       text|json|markdown|html|junit|none (default text)
--output, -o FILE      write the report to FILE (`-` means stdout)
--no-store             do not record this run in the history database
```

`watch` takes the same crawl flags plus `--every 30m|6h|1d`, `--notify-webhook URL`,
`--once`. `diff` and `report` take `--format`, `--output` and (for `diff`) `--fail-on`,
`--site`; `report` also takes `--diff-with-previous`.

## The issues Linkkeep reports

Every finding has a stable `code` and a severity. `--fail-on error` (the default) makes the
command exit `1` if any finding is at or above that level; `never` disables failing.

| code | severity | meaning | how to make it stop |
| :-- | :-- | :-- | :-- |
| `broken` | error | target answered 4xx/5xx | fix the link, or `--ignore-status 404` for a whole class |
| `unreachable` | error | DNS failure, connection refused, TLS error or timeout; the reason is recorded | fix DNS/host, or `--exclude` it |
| `redirect_loop` | error | the chain comes back to a URL already seen (often a login wall) | fix the redirect |
| `redirect_to_error` | error | the chain ends in 4xx/5xx, so the link "works" in a browser and is still broken | repoint the link at the final URL |
| `redirect_chain` | warning | more hops than `max_redirect_hops` (3) | collapse the chain |
| `redirect_http_downgrade` | warning | an `https` URL that redirects down to `http` | point links at the final `https` URL |
| `mixed_content` | warning | `http` resource referenced from an `https` page (browsers block these) | use `https` for the asset |
| `missing_fragment` | warning | `#anchor` does not exist on the target page (only with `--check-fragments`) | restore the anchor or drop the fragment |
| `redirect_temporary_permanent_internal` | info | internal link answered 302/307 where 301 is probably meant | change the redirect, or `detection.temporary_redirect_hint: false` |
| `slow` | info | response time above `detection.slow_ms` (5000) | speed it up, or raise the threshold |
| `blocked_by_robots` | info | a link we could not check because robots.txt disallows it | nothing — it is a note about coverage |

Severity is a property of the code, so `--fail-on warning` catches mixed content and slow
drift too, and `--fail-on info` catches everything.

Each finding records: the **source page** it was seen on, the **element and attribute**
that carried it (`<a href>`, `<img src>`, `srcset`, …) and the **link text**, the
**target**, the **final URL** after redirects, the **status**, the **redirect chain**, the
**reason** (for network failures) and the **time it was found**. That is what makes the
HTML/JSON reports actionable instead of a shrug.

`slow` measures server response time only: time Linkkeep spends waiting for its own rate
limiter is tracked separately (`waited_ms`) and never counts as slow.

## Configuration

`linkkeep init` writes a fully commented file; `linkkeep.yaml` in the current directory (or
any parent) is picked up automatically, or pass `--config FILE`. Precedence is
**built-in defaults < config file < CLI flags**, merged section by section — a file that
sets `crawl.rate` does not reset your other crawl defaults.

```yaml
site: https://example.com
user_agent: "Linkkeep/0.1 (+https://github.com/YOUR-OWNER/linkkeep)"
database: ~/.local/share/linkkeep/linkkeep.sqlite
respect_crawl_delay: true

crawl:
  max_pages: 1000
  max_depth: null          # unlimited
  concurrency: 10
  rate: 5                  # requests/second, per host
  timeout: 15
  retries: 1
  max_redirect_hops: 3
  max_response_size: 2000000   # 2 MB parse cap

rules:
  include_subdomains: false
  include: []              # e.g. ["glob:https://example.com/blog/**"]
  exclude: ["re:/\\.(pdf|zip|png|jpg|css|js)$"]
  ignore_status: [999]
  check_fragments: false
  sitemap: false
  follow_external: true
  allow_private: false
  ignore_robots: false
  url_trailing_slash: keep # keep | strip | add
  url_sort_query: false

detection:
  slow_ms: 5000
  mixed_content: true
  temporary_redirect_hint: true

auth:
  headers:
    Authorization: "env:LINKKEEP_AUTH"
  cookies:
    session: "env:LINKKEEP_SESSION"

output:
  fail_on: error           # error | warning | info | never
  format: text             # text | json | markdown | html | junit | none
  color: true
  quiet: false
  verbose: false

watch:
  every: 6h
  notify_webhook: null
```

Defaults worth knowing about: the built-in `rules.exclude` is **empty** (the generated
`linkkeep.yaml` suggests asset and `/logout` patterns, because they cause the most noise).
`linkkeep report` defaults to `--format markdown`, `linkkeep crawl` to `--format text`.

### Patterns

`include` / `exclude` values are treated as regular expressions when they parse as one,
and otherwise as globs against the whole URL. Prefix with `re:` or `glob:` to force one or
the other (`glob:` supports `*`, `?`, `[]` and `**`). `include` is applied first (if set, a
URL must match it), then `exclude` removes. Excluded links are still *recorded* — with
`error: "excluded by pattern"` and `checked: false` — so the report shows what was skipped.

### Secrets

`auth.headers` / `auth.cookies` values may be literal or `env:VAR`. Values are resolved
when the request is built and are **never** printed, logged, or written to the database;
only the header/cookie *names* are stored with the run. Reports and tracebacks redact them.
Use `LINKKEEP_*` variables and let CI provide them.

An entry whose `env:VAR` is unset is **dropped** (no blank `Authorization` header is sent)
and `crawl`/`watch` print a warning naming the variable, not its value:

```console
warning: header Authorization: env:LINKKEEP_AUTH is not set - requests will be sent without it
```

### Webhook payload

`watch --notify-webhook URL` POSTs this JSON body **only when the newest crawl has new
issues**; a failing webhook never breaks the loop (it prints one warning line and carries
on):

```json
{
  "event": "linkkeep.new_issues",
  "site": "https://example.com",
  "run_id": 12,
  "previous_run_id": 11,
  "summary": "https://example.com: 2 new, 0 fixed, 7 persisting",
  "new_issues": [
    {
      "code": "broken", "severity": "error", "fingerprint": "a1b2c3d4e5f60708",
      "source_url": "…", "target_url": "…", "final_url": "…", "status": 404,
      "reason": null, "element": "a", "attribute": "href", "link_text": "Shop",
      "redirect_chain": [], "found_at": "2026-10-10T09:12:03+00:00"
    }
  ],
  "counts": {"pages": 214, "links": 911, "errors": 5, "…": "same counters object as the JSON report"},
  "fail_on": "error",
  "report_url": null
}
```

`new_issues[]` items are exactly the `issues[]` objects from [JSON report
schema](#json-report-schema), and `counts` is that report's `counts` object, so one
receiver handles both. `report_url` is reserved (always `null` in v0.1) for when Linkkeep
can publish a hosted report.

## Using it in CI

### Weekly crawl that fails when something new breaks

The trick is to keep the SQLite history between runs (the cache action does that) and let
`diff` decide whether the job fails: a new error is a regression, an old one is already
being tracked. [`examples/github-actions-linkcheck.yml`](examples/github-actions-linkcheck.yml) in this repo is exactly this file, ready to copy.

```yaml
name: link check
on:
  schedule: [{cron: "17 6 * * 1"}]   # Mondays, 06:17 UTC
  workflow_dispatch:
permissions:
  contents: read
jobs:
  links:
    runs-on: ubuntu-latest
    env:
      DB: ${{ runner.temp }}/linkkeep-cache/history.sqlite
    steps:
      - uses: actions/cache@v4
        with:
          path: ${{ runner.temp }}/linkkeep-cache
          key: linkkeep-${{ github.run_id }}      # fresh key each run...
          restore-keys: linkkeep-                 # ...restored from the previous run
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pipx install linkkeep
      - name: crawl
        # --fail-on never: the crawl itself must not fail the job, the diff decides
        run: linkkeep --db "$DB" crawl https://example.com --fail-on never --format json --output report.json
      - name: report to the job summary, fail on NEW errors
        run: |
          if [ -f "$DB" ]; then
            linkkeep --db "$DB" diff --format markdown >> "$GITHUB_STEP_SUMMARY"
          else
            echo "first crawl: nothing to compare against yet" >> "$GITHUB_STEP_SUMMARY"
          fi
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: linkkeep-report, path: report.json}
```

`diff` exits `1` when any **new** finding reaches `--fail-on` (default `error`), which is
what fails the job. Use `--fail-on warning` to also block on mixed content and redirect
chains, or `--fail-on never` to always report and never fail.

For a per-PR check on a preview deployment, the same two steps work with the preview URL
and `--allow-private` if it is only reachable from the CI network. Locally, cron is fine:

```cron
17 6 * * 1  linkkeep crawl https://example.com --format none; linkkeep diff -q | mail -s "links" team@example.com
```

### JUnit in Jenkins/GitLab

```bash
linkkeep crawl https://example.com --format junit --output linkkeep.xml --fail-on warning
```

## Politeness

* **robots.txt by default.** `User-agent`/`*` rules are honoured for both crawling and
  external status checks. A URL disallowed for us is not fetched at all; it is reported as
  `blocked_by_robots` (info) so you know the coverage gap. `--ignore-robots` overrides, and
  prints a warning, because it should feel like a deliberate choice.
* **`Crawl-delay`** is respected when present (`respect_crawl_delay: false` to opt out).
* **Rate limit:** 5 requests/second *per host*, as a token bucket whose capacity is 1 —
  that is a strict minimum interval of 200 ms between requests to the same host, not a
  burst allowance.
* **Concurrency:** 10 simultaneous requests overall.
* **Timeout** 15 s, **retries** 1 (only for timeouts, connection errors, 429 and 5xx).
* **Max 1000 internal pages** per run, no depth limit, and HTML bodies are only read up to
  2 MB (the fetch still reports the status; `truncated` is noted).
* **User-Agent** identifies the tool and a contact URL:
  `Linkkeep/0.1.0 (+https://github.com/YOUR-OWNER/linkkeep)`. Change it, don't hide it.
* External links are checked with `HEAD`, falling back to `GET` when `HEAD` is rejected —
  and never crawled.
* **Ctrl+C** once = finish in-flight requests, flush what we have to the database, mark the
  run `interrupted`, exit. Twice = give up on in-flight requests (the last flush is still
  saved). Re-running `runs` shows the interrupted run, and `prune`/the next crawl mark
  stale rows.

### Refusing to poke your internal network

Links are user- or CMS-controlled, so Linkkeep will not fetch loopback or private-address
hosts (`127.0.0.1`, `10/8`, `192.168/16`, `169.254/16`, `fc00::/7`, `::1`, `.local`, …)
when crawling a public site. Those links are recorded as skipped with the note *"skipped:
private or loopback address (use --allow-private)"*. If a seed itself is private, Linkkeep
refuses and tells you to pass `--allow-private`. That flag is meant for local dev and CI
preview environments — see the demo and the integration tests, which use it to crawl
`127.0.0.1`.

## How a URL is judged

1. **Extract** from `a[href]`, `link[href]`, `img[src]`, `img[srcset]`, `source[srcset]`,
   `script[src]`, `iframe[src]`, `source[src]` — parsed with `selectolax`, so real-world
   malformed HTML is fine. `srcset` candidates are expanded into separate links.
2. **Resolve** against the page URL, honouring `<base href>`; `../x` and `?y` forms work.
3. **Normalise**: strip fragments from the fetch target (the fragment is kept separately for
   `--check-fragments`), drop default ports (`:80`, `:443`), IDNA-encode hosts, apply the
   trailing-slash policy, optionally sort query parameters, and dedupe.
4. **Classify** as internal or external by registrable host. By default the hosts must match
   exactly (so `www.example.com` and `example.com` are treated as the same site — a
   deliberate deviation in favour of not crawling half the web twice).
   `--include-subdomains` widens internal to "any subdomain of the registrable domain".
5. **Ignore** `mailto:`, `tel:`, `javascript:`, `data:`, `sms:` and friends — counted, never
   fetched.

## Storage

One SQLite file, standard library only, no ORM. On Linux the default is
`~/.local/share/linkkeep/linkkeep.sqlite` (macOS: `~/Library/Application Support/linkkeep`,
Windows: `%LOCALAPPDATA%\linkkeep`); override with `--db`, `database:` in the config, or
`LINKKEEP_HOME`. WAL mode, foreign keys on, and every write is one transaction — losing
power mid-crawl leaves the last flush intact, not a corrupt file.

| table | contents |
| :-- | :-- |
| `runs` | one row per crawl: site, seeds, status, start/finish, full counters, the redacted config used |
| `pages` | one row per internal page: depth, status, final URL, duration, content type, size, links found/checked, `crawlable` |
| `links` | one row per link occurrence: source, target, kind, element/attribute/text, status, final URL, duration, error, scheme, `checked` |
| `issues` | one row per finding, `UNIQUE (run_id, fingerprint)` |

`PRAGMA user_version` is the schema version and migrations are applied one version at a
time inside a transaction (see `MIGRATIONS` in `linkkeep/storage.py`). A database written
by a newer Linkkeep is refused with a clear message instead of being downgraded.
`first_seen`/`runs_seen` queries over `issues.fingerprint` are what make *broken since*
and the CI diff cheap.

## JSON report schema

`--format json` is versioned: the top-level `schema_version` integer changes only when
fields are added or repurposed (fields are never silently renamed; removals require a minor
version bump). Currently `schema_version: 1`.

```jsonc
{
  "schema_version": 1,
  "tool": {"name": "linkkeep", "version": "0.1.0"},
  "generated_at": "2026-10-10T09:12:31+00:00",   // render time, ISO-8601 UTC
  "site": "https://example.com",
  "run": {                                        // null-ish fields are always present
    "id": 7, "site": "…", "status": "completed",  // completed | interrupted | failed | running
    "started_at": "…", "finished_at": "…", "duration_seconds": 41.2,
    "seeds": ["https://example.com"], "notes": ["sitemap added 5 URL(s)"],
    "config": { "auth": {"redacted": true}, "crawl": {…}, "rules": {…} }
  },
  "counts": {
    "pages": 214, "links": 911, "checked": 903, "skipped_scheme": 27,
    "fragment_only": 4, "excluded": 5, "robots_blocked": 3,
    "errors": 3, "warnings": 1, "infos": 2,
    "by_type": {"broken": 2, "slow": 1, …}
  },
  "issues": [{
    "code": "broken", "severity": "error",
    "fingerprint": "a1b2c3d4e5f60708",             // type + source + target, stable across runs
    "source_url": "…", "target_url": "…", "final_url": "…", "status": 404,
    "reason": null, "element": "a", "attribute": "href", "link_text": "Handbook",
    "redirect_chain": [{"url": "…", "status": 301, "location": "…"}],
    "found_at": "…", "description": "…", "summary": "HTTP 404"
  }],
  "diff": null,                                     // or {run_a, run_b, new[], fixed[], persisting[]}
  "pages": [{"url": "…", "depth": 1, "status": 200, "duration_ms": 184, "crawlable": true, …}],
  "links": [{"source_url": "…", "target_url": "…", "kind": "internal", "checked": true, …}]
}
```

`linkkeep runs --json` gives the same `run`/`counts` shape as an array. Everything is
UTF-8, two-space indented, newline-terminated.

## Using it from Python

The HTTP layer is an injected interface, which is why this project's own tests need no
mocks of `httpx` at the crawl level:

```python
import asyncio
from linkkeep.config import Config
from linkkeep.crawler import Crawler
from linkkeep.fetcher import HttpxFetcher
from linkkeep.storage import Storage

config = Config.from_dict({"crawl": {"max_pages": 200}, "rules": {"check_fragments": True}})
fetcher = HttpxFetcher(timeout=10.0, user_agent=config.user_agent)
storage = Storage("history.sqlite")

report = asyncio.run(Crawler(config, fetcher, storage=storage).run(["https://example.com"]))
print(report.counts)                       # Counts(pages=201, links=842, errors=3, …)
for issue in report.issues:
    print(issue.code, issue.source_url, "->", issue.target_url)
```

Subclass `linkkeep.fetcher.Fetcher` (four methods: `fetch`, `check_link`, `close`,
context-manager) to feed the crawler from a cache, a browser session or a test double.
`linkkeep.issues` holds the detectors — one function per code, registered in `DETECTORS`.

## Limitations in v0.1

* **No JavaScript rendering.** Links that only exist after a script runs (SPAs, infinite
  scroll, `<a>` built by a framework) are invisible; we parse the HTML the server returned.
  This is the single biggest gap and the main reason `sitemap` exists.
* Only the attributes listed in [How a URL is judged](#how-a-url-is-judged) are extracted —
  no CSS `url(…)`, no `<svg><use href>`, no `<area href>` image maps, no `poster`/`data-src`
  lazy-load attributes (some lazy-load patterns are better solved with an `include` for the
  real URL, and adding an element to `extract.py` is a two-line change with a test).
* `POST`-only flows, forms, and authenticated multi-step journeys are out of scope;
  `auth.headers` gets you into protected pages, nothing more.
* Fragment checking (`--check-fragments`) compares against `id`/`name` attributes on the
  fetched HTML; it cannot see anchors created by scripts.
* One process per run: the rate limiter is per-process, so two concurrent Linkkeep runs on
  the same host each get their own 5 req/s.
* No dedupe of "same content at two URLs", no `rel=nofollow` respect (deliberate: nofollow
  is a hint for search engines, not a politeness rule), and no sitemap `lastmod`-based
  incremental crawling yet.
* Mixed-content detection looks at scheme, not at whether the origin has an HSTS entry.
* HTML and JUnit reports are point-in-time files; there is no web UI.

## Roadmap

1. **Playwright backend** (`--renderer playwright`) so JS-rendered links are found too,
   with the same detectors and history. The fetcher interface is designed for this.
2. Incremental crawls driven by sitemap `lastmod`, and `--resume` for interrupted runs.
3. Per-URL mute files (`linkkeep ignore add …`) instead of only patterns.
4. Anchors/redirect hygiene suggestions: "34 links still point at `/old-url`; add a 301".
5. `linkkeep export --format github-comment --pr` with a sticky comment and a collapsible
   body.
6. Optional lightweight web UI over the SQLite file.

Decided against, and unlikely to change: distributed crawling, JavaScript-rendered page
*interaction*, and a hosted dashboard.

## Design decisions

Made without asking, and worth knowing about:

* `www.`-tolerant internal check by default (documented above), because most sites 301
  between the two and treating them as different hosts produces thousands of bogus external
  links.
* `redirect_temporary_permanent_internal`, `slow` and `blocked_by_robots` are `info`, not
  `warning`, so the default `--fail-on error` never blocks a build over style advice.
* Trailing-slash policy is `keep` by default: guessing `strip` or `add` changes what you
  fetch, and a wrong guess invents 404s. It only affects dedupe unless you set it.
* Findings are keyed by fingerprint rather than by row identity, which makes diffs and
  *broken since* trivial SQL instead of a matching heuristic.
* External links are memoised per target URL, so a footer link repeated on 300 pages costs
  one request.
* `linkkeep report` prints markdown by default (it is the format you paste somewhere), while
  `crawl` prints text.
* The `--format none` mode exists so CI can crawl for the exit code alone.
* `interrupted` is a first-class run status, and stale `running` rows from a killed process
  are marked `interrupted` on the next command.
* Config loading prunes `None` overrides at every depth, so `--max-pages 5` does not wipe
  the rest of your `crawl:` section.
* Only seven runtime dependencies, and no ORM, so `pipx install linkkeep` stays cheap. An
  ORM would have to learn our schema anyway, and SQLite is in the standard library.
* The demo/fixture site lives **inside the package** (`linkkeep.fixture_site`) rather than in
  `tests/`, so the integration tests, `make demo`, the README samples and CI all exercise the
  same site - and a user can reproduce a bug report against a known-broken site in one command.
* Redirects are followed hop-by-hop instead of letting `httpx` do it: the chain, the loop
  detection and the "redirect to error" distinction all need to see each status code.
* `slow` subtracts the rate limiter's wait from the measured duration, otherwise a full
  concurrency window reports every page as slow. The number is server time.
* Reports never touch the database: `ReportData` is built by `from_storage`/`from_crawl` and
  passed to renderers, which keeps the formats trivially testable (and lets the Python API
  render a report it built itself).
* `crawl` records findings even with `--no-store`, but `diff` and *broken since* then have
  nothing to compare against - storing is the default because the history is the product.
* `robots.txt` is respected for crawling decisions; external links are still status-checked
  (a `403` on an off-site link is our problem to report, not theirs to hide).
* The seed URL in the run record is the normalised first seed, so `--site` matching works
  after a redirect from `http://` or a trailing-slash difference.

## Development

```bash
make install     # venv + dev dependencies + pre-commit hooks
make test        # pytest with coverage, fails under 85%
make lint        # ruff check
make fmt         # ruff format
make typecheck   # mypy --strict on src/
make goldens     # regenerate tests/goldens (review the diff: it is user-visible output)
make demo        # real CLI against the bundled fixture site
make check       # lint + format check + typecheck + tests
make build       # sdist + wheel
```

Tests never touch the internet: `FakeFetcher` (a route-table implementation of the fetcher
interface) covers crawl behaviour, `httpx.MockTransport`/`respx` cover the HTTP layer,
`RateLimiter` runs on an injected clock, and `tests/test_integration.py` runs the whole CLI
against `linkkeep.fixture_site` on `127.0.0.1`.

See [CONTRIBUTING.md](CONTRIBUTING.md) — including how to add a new issue detector — and
the [changelog](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE). `YOUR NAME` in that file is a placeholder you should edit
before publishing.
