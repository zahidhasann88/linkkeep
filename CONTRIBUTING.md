# Contributing to Linkkeep

Thank you for spending part of your day on a link checker.

Everything below is real: the commands work in this repository, and CI runs exactly them.
If a step here does not match the code, that is a bug - open an issue or fix it in the same
PR.

## Contents

- [Before you start](#before-you-start)
- [Setup](#setup)
- [The one-minute tour](#the-one-minute-tour)
- [Day-to-day commands](#day-to-day-commands)
- [Adding an issue detector](#adding-an-issue-detector)
- [Testing rules](#testing-rules)
- [Style](#style)
- [Commit messages and pull requests](#commit-messages-and-pull-requests)
- [Reporting a bug or asking for a feature](#reporting-a-bug-or-asking-for-a-feature)
- [Maintainers: releasing](#maintainers-releasing)

## Before you start

For anything beyond a typo, please open an issue first. Linkkeep is small and opinionated
(the [README](README.md#design-decisions) lists the opinions), and a two-line conversation
before you write code saves both of us a review cycle. If your feature needs a headless
browser, a plugin system, or a new dependency, say so up front - those are decisions, not
details.

## Setup

Python 3.10 or newer. That is all - the sandbox in CI uses 3.12.

```bash
make venv install       # .venv + linkkeep[dev] editable + pre-commit hooks
make check              # ruff, ruff format --check, mypy strict, pytest with the 85% gate
make demo               # crawl the bundled broken site with the real CLI
```

If you would rather not use make:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -q
```

`make demo` needs no network: the crawler talks to a local HTTP server started inside the
test process (`src/linkkeep/fixture_site.py`) - a deliberately broken site with 404s, a
redirect loop, an over-long redirect chain, robots-blocked paths and a slow page.

## The one-minute tour

```text
src/linkkeep/
  cli.py        Typer app: commands, flags, exit codes, error rendering. No business logic.
  config.py     Pydantic models, YAML discovery, defaults, User-Agent, data dir
  models.py     enums (IssueType, Severity), Issue, PageRecord, LinkRecord, fingerprint
  urlnorm.py    URL resolution + normalisation - the purest module in the repo
  extract.py    HTML -> ExtractedLink list (selectolax) + anchor collection for fragments
  fetcher.py    the Fetcher interface + HttpxFetcher (manual redirect following)
  ratelimit.py  token bucket per host, injectable clock and sleep
  robots.py     robots.txt cache, decisions, crawl-delay, sitemap hints
  crawler.py    the orchestrator: queue, politeness, detection, storage flushes
  issues.py     detectors - where most contributions land
  diff.py       run vs run: NEW / FIXED / PERSISTING
  storage.py    sqlite3, schema versions, migrations, queries
  report/       text, markdown, html, json, junit + the shared ReportData
  fixture_site.py  the local test site (shipped so tests and demos need no network)
tests/          pytest; goldens in tests/goldens/; tools/update_goldens.py to regenerate
tools/demo.py   the script whose output the README quotes
```

Three rules hold the design together:

1. **The fetcher is an interface.** `Crawler` receives a `Fetcher`; tests pass a fake.
   Anything you write should not know or care whether the bytes came from httpx or a dict.
2. **Detectors are pure functions.** `(LinkContext, DetectionSettings) -> list[Issue]`.
   No I/O, no fetching, no printing. If a rule needs data the context does not have, the
   crawler should provide it - or the rule belongs at page level, in the crawler.
3. **The database is history, not state.** A run is immutable once written; diff and
   `broken since` read that history. Never mutate an old run.

## Day-to-day commands

| command | what it does |
| :-- | :-- |
| `make test` | `pytest -q --cov-fail-under=85` |
| `make lint` | `ruff check src tests tools` |
| `make fmt` | `ruff format` + `ruff check --fix` (then re-run the tests) |
| `make typecheck` | `mypy` (strict, over `src/linkkeep`) |
| `make goldens` | regenerate `tests/goldens/*` after intentional report changes |
| `make demo` | run the CLI against the fixture site |
| `make check` | lint + format check + typecheck + tests - what CI runs |
| `make build` | `python -m build` into `dist/` |
| `python tools/check_yaml.py` | parse the repo's YAML and load `examples/linkkeep.yaml` |

## Adding an issue detector

Suppose we want a new issue type: a linked asset (PDF, video, zip) that is bigger than a
threshold, because a 40 MB PDF behind a nav link is a usability bug. The steps below are
the full list - missing one is the most common reason a detector PR needs a second round.

**1. Name it in `models.py`.** The value is the wire format: it appears in reports, in
the database and in CI pipelines, so it never changes afterwards.

```python
class IssueType(StrEnum):
    ...
    ASSET_TOO_BIG = "asset_too_big"
```

**2. Give it a severity and a description** (also `models.py`). `error` means "a user is
probably hitting a dead end" - be conservative; `--fail-on error` is the CI default, so an
over-eager `error` turns pipelines red. Our example is a `warning`.

```python
SEVERITY_BY_TYPE: dict[IssueType, Severity] = {
    ...
    IssueType.ASSET_TOO_BIG: Severity.WARNING,
}

DESCRIPTION_BY_TYPE: dict[IssueType, str] = {
    ...
    IssueType.ASSET_TOO_BIG: "linked asset is larger than the configured limit",
}
```

**3. Write the rule** in `issues.py`. Pure, cheap, and quiet when uncertain: no size
header means no issue, not a guess.

```python
def detect_asset_too_big(ctx: LinkContext, settings: DetectionSettings) -> list[Issue]:
    """A linked asset whose reported size exceeds ``settings.max_asset_bytes``."""
    limit = settings.max_asset_bytes
    size = ctx.result.size_bytes
    if limit <= 0 or size is None or size <= limit:
        return []
    return [
        build_issue(
            IssueType.ASSET_TOO_BIG,
            source_url=ctx.source_url,
            target_url=ctx.target_url,
            final_url=ctx.result.final_url or None,
            status=ctx.result.status,
            reason=f"{size} bytes > limit of {limit} bytes",
            element=ctx.element,
            attribute=ctx.attribute,
            link_text=ctx.link_text,
        )
    ]
```

**4. Register it** in `DETECTORS` (link-level, runs for internal pages and external links)
so `run_link_detectors` picks it up:

```python
DETECTORS = (
    detect_unreachable,
    detect_broken,
    detect_redirect_problems,
    detect_mixed_content,
    detect_slow,
    detect_asset_too_big,
)
```

Page-level rules - ones that need the whole document, like `missing_fragment` or
`blocked_by_robots` - are not in that tuple; they are called from `crawler.py` where the
page's data is in hand.

**5. Add any tunable** to `DetectionSettings` (`issues.py`), to the Pydantic `Detection`
model (`config.py`), to `settings_from_config` (`crawler.py`), to the CLI as a flag if it
is worth typing at a keyboard, and to `INIT_CONFIG_YAML` plus
[`examples/linkkeep.yaml`](examples/linkkeep.yaml) if it deserves a comment. A threshold
that ships without a comment explaining its default will be asked about.

**6. Test it** in `tests/test_issues.py`:

```python
def test_asset_too_big_reports_only_above_the_limit() -> None:
    ctx = link_context("https://example.com/big.pdf", size_bytes=5_000_000)
    settings = DetectionSettings(max_asset_bytes=1_000_000)
    issues = detect_asset_too_big(ctx, settings)
    assert [i.code for i in issues] == ["asset_too_big"]
    assert issues[0].severity is Severity.WARNING

def test_asset_too_big_is_quiet_without_a_content_length() -> None:
    ctx = link_context("https://example.com/mystery.pdf", size_bytes=None)
    assert detect_asset_too_big(ctx, DetectionSettings(max_asset_bytes=1)) == []
```

Also assert the end-to-end behaviour in `tests/test_cli.py` or `tests/test_integration.py`
when the flag is user-visible: a detector that works but cannot be reached from the CLI is
half a feature.

**7. Reports usually need nothing.** The text/markdown/JSON/HTML/JUnit renderers group by
`issue.code` generically, and the summary counts by severity. Two exceptions:
`--fail-on` behaviour (nothing to do - severities are compared, not enumerated) and the
golden files. If your detector can fire in the fixture-site scenario, run `make goldens`
and read the diff.

**8. Document it.** Add a row to the issue reference table in `README.md`: code, severity,
what triggers it, and a `fix:` hint. The table is the contract for users, and the
`fix:` line is the part people actually act on.

Then check it against the fixture site:

```bash
.venv/bin/python tools/demo.py --outdir /tmp/demo   # or: make demo
```

## Testing rules

These are not preferences; CI enforces them.

- **No test may touch the real internet.** Unit tests inject `FakeFetcher` or stub
  `HttpxFetcher`; anything needing real HTTP uses `respx`/`httpx.MockTransport`; the
  integration suite talks to the in-process fixture server on `127.0.0.1`. A test that
  hits a real domain is flaky, rude and wrong.
- **Determinism.** Dates come from the injected clock (`NOW` in `tests/conftest.py`), so
  goldens and `broken since` lines never drift. Never call `datetime.now()` in a test,
  and never encode wall-clock assumptions in product code.
- **Coverage gate 85%.** `pyproject.toml` enforces it and CI repeats it. New modules must
  come with tests, not with a lower threshold.
- **Report formats are golden-tested.** Deliberate output changes go through `make goldens`
  so the diff is reviewed as a behaviour change.

`pytest -q` is fast (~45 s); `pytest tests/test_integration.py -q -k crawl` is the quickest
way to see a change work end to end.

## Style

The rules CI enforces: line length 100, `ruff format`, `mypy --strict` on `src/linkkeep`
(no `Any` leaking out of public functions, no untyped defs), no `assert` outside tests.

The rules humans care about, in rough priority order:

1. **Explain the deviation.** If you do something unusual (SQLite migration outside a
   transaction, manual redirect following, `re:`-first pattern sniffing), write the why
   next to the code. Unexplained cleverness gets reverted.
2. **Errors are for humans.** `raise ConfigError("no such run: 42 (the latest is 37)")`,
   not a traceback. The CLI prints `error: <message>` and exits 2; tracebacks appear only
   with `--verbose`.
3. **Never print a secret.** Auth values are resolved at request time and stored as names
   only. If you add a config field that can hold a credential, add it to the redaction
   path and a test that asserts the value is absent from output.
4. **Keep dependencies minimal.** Adding one needs a section in the PR description and a
   sign-off from a maintainer. `urllib3`, `beautifulsoup4`, `requests`, an ORM and a
   testing framework are deliberately absent - see the README.
5. **Comments say why, not what.** A docstring should state the contract and the surprise.

## Commit messages and pull requests

[Conventional Commits](https://www.conventionalcommits.org/), imperative subject, ≤ 72
characters, no trailing period. The type drives the changelog, so `fix`/`feat` matter and
`chore` should not hide a behaviour change.

```text
feat(diff): show "broken since" for issues persisting across runs
fix(crawler): exclude rate-limiter wait from the slow detector
docs(readme): document the --ignore-status escape hatch
test(storage): cover the schema_version 1 -> 2 migration
refactor(fetcher): retry with GET only after a 405 on HEAD
```

Allowed scopes: `cli`, `config`, `crawler`, `fetcher`, `extract`, `issues`, `models`,
`urlnorm`, `storage`, `diff`, `report`, `robots`, `ratelimit`, `tests`, `docs`, `deps`,
`ci`, `chore`.

One PR per idea. A PR that adds a detector, changes the JSON schema *and* restyles the
terminal output is three PRs; the schema change in particular needs to be visible on its own
(bump `schema_version` in the same commit as the key change).

The checklist in [.github/pull_request_template.md](.github/pull_request_template.md) is
short on purpose. `make check` and `make demo` cover most of it.

## Reporting a bug or asking for a feature

Use the [issue forms](.github/ISSUE_TEMPLATE). For a bug the two things that matter most
are the **exact command** and the **HTML of the page or element** involved - link-checking
bugs live in markup, and a maintainer cannot fetch your site. Redact anything secret;
paste `linkkeep --version` output; include the traceback only if you ran with `--verbose`.

For a false positive, say what you expected in one sentence. Several "bugs" are documented
trade-offs (no JS-rendered links, `<area>` not followed, `srcset` candidates treated as
links) and the answer may be a config key rather than a code change.

Security reports go through the private flow in [SECURITY.md](SECURITY.md), never a public
issue.

## Maintainers: releasing

1. Update `CHANGELOG.md`: move `## Unreleased` entries under a new `## [<version>]` heading
   with the date, and add the comparison link at the bottom.
2. Bump the version in `pyproject.toml` and `__version__` in `src/linkkeep/__init__.py`. Tests assert the version string
   appears in `--version` output, and the reports print it.
3. `make check && make build` - install the wheel in a clean venv and run
   `linkkeep --version` plus a real crawl against the fixture site.
4. `git tag -s v<version> && git push origin v<version>` - `.github/workflows/release.yml`
   builds and creates the GitHub Release with distribution assets. PyPI publication uses
   trusted publishing and requires the `pypi` environment, a matching publisher, and
   the repository Actions variable `PUBLISH_TO_PYPI` set to `true`.
5. Check every release job and verify the GitHub assets and PyPI installation.

Breaking changes to the JSON report schema, the exit-code contract, the config file
semantics or a stable issue code are `minor` bumps in v0.x, and the README's compatibility
notes must say what changed. If you have to edit a migration, add a new one - never
rewrite an existing `MIGRATIONS` entry.
