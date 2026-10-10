# Security policy

Linkkeep is a crawler that makes requests with credentials you give it and writes what it
finds into a SQLite file. That combination is the security surface, so it is documented here
in full - including the parts that are a bad idea by design.

## Reporting a vulnerability

Please use [GitHub's private vulnerability reporting](https://github.com/YOUR-OWNER/linkkeep/security/advisories/new)
rather than a public issue. If you cannot or do not want to use it, write to the maintainer
address on the repository's security page.

What we will do:

- acknowledge within **3 business days**;
- ship a fix or a documented mitigation for anything rated moderate or higher within
  **90 days**, and tell you when it is released;
- credit you in the advisory unless you would rather stay anonymous.

What helps us act fast: the exact version (`linkkeep --version`), the command and config you
used, and a description of the trust boundary you crossed. A runnable repro is welcome but do
not post one publicly.

## What is considered a vulnerability

| report | rating | why |
| :-- | :-- | :-- |
| Secrets leaking into reports, logs, the database or tracebacks | high | the whole point of `env:` indirection and `redact_*` is that they do not |
| SSRF from a crawl whose policy said it would not leave the site (e.g. a public site's link reaching a private address without `--allow-private`) | high | the guard is in `urlnorm.is_private_host` and the crawler refuses such seeds |
| Injection into a generated report (`html`/`markdown`/`junit` are artifacts people open, so link text and URLs must be escaped) | medium-high | see `report/html.py` escaping and `junit.py`'s `esc()` |
| SQLite file permissions or path traversal from a `--db`/`output` value | medium | we create the parent directory and open the file with default umask, so a shared runner needs care |
| Resource exhaustion that makes Linkkeep hammer a host past its `rate`/`concurrency`/`max_pages` limits | medium | politeness limits are functional requirements |
| Robots-bypass other than the documented `--ignore-robots` | medium | the default must be honourable |
| A crash, or a stack trace without `--verbose` | not security | open a normal issue |
| "Linkkeep checked a URL I did not ask it to" where that URL was a legitimate `<a href>` on a crawled page | not security | that is what crawling means; use `--include`/`--exclude` to narrow |

## Hardening notes for users

- **Secrets.** `auth.headers`/`auth.cookies` accept `env:VAR`; values are resolved per request,
  are never printed, and only *names* are stored with the run (`to_stored_dict` redacts). They
  are still sent to whatever `--config` says and to the hosts you crawl: do not point a
  credentialed config at a site with third-party links you trust less than your own. An unset
  variable drops the header and warns, so nothing leaks downstream if CI forgets a secret.
- **The database.** `linkkeep.sqlite` contains URLs of pages you may not have wanted recorded
  (admin paths, `?token=…` links). `prune --older-than` and `--vacuum` exist for a reason; on
  a shared machine, keep it in your own account's data directory, not a world-readable temp
  path. CI runners: the history cache is readable by anyone with access to the workflow's
  artifacts.
- **`--allow-private`** disables the only thing standing between a typo'd seed URL and a
  request to a metadata service (`169.254.169.254`) or an internal admin port. Use it on a
  local dev box or an isolated CI network, never with untrusted input. It is also
  the right flag for `localhost` preview deployments; the choice is deliberate and explicit.
- **Reports are artifacts.** HTML and Markdown can carry page text (link text, titles) from
  the crawled site into a file that someone opens. Treat a report from an untrusted site as
  untrusted input. JUnit XML is escaped, but CI systems that render it as HTML deserve a look.
- **No sandbox.** Linkkeep trusts the HTML it parses; `selectolax`/lexbor handles malformed
  input, but a hostile site is still an attack surface. Crawl sites you have a relationship
  with, and cap `--max-pages`, `--timeout`, and `--concurrency` if you crawl on someone else's
  behalf.
- **Outbound webhook.** `watch --notify-webhook` POSTs a summary that includes URLs from your
  site. Send it to a destination you control, over https.

## Supported versions

| version | supported |
| :-- | :-- |
| 0.1.x | :white_check_mark: |
| < 0.1 | :x: - pre-release, use the latest |

While the project is 0.x, security fixes land on the latest release; we do not backport to
older 0.x unless the issue is severe and a release is impractical.

## Scope

This policy covers this repository's code, workflows and published distributions. It does
not cover the behaviour of the sites you crawl, your CI provider, or the security of
`httpx`, `selectolax`, `protego`, `typer`, `rich`, `pydantic` or `pyyaml` - though Dependabot
opens PRs here when those need updating, and you should take them.
