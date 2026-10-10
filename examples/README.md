# Examples

Two files, both usable as-is:

| file | what it is |
| :-- | :-- |
| [`linkkeep.yaml`](linkkeep.yaml) | a complete config file: every key Linkkeep reads, with comments. `linkkeep init` writes a shorter version of this. |
| [`github-actions-linkcheck.yml`](github-actions-linkcheck.yml) | a GitHub Actions workflow: crawl weekly, keep the history in a cache, and fail only when something **new** breaks. |

## Using the config file

```bash
cp examples/linkkeep.yaml ./linkkeep.yaml
$EDITOR linkkeep.yaml            # set `site:` to your domain
linkkeep crawl                   # picks up ./linkkeep.yaml automatically
linkkeep --config examples/linkkeep.yaml crawl https://example.com   # or point at it
```

Values shown as `https://example.com` are placeholders. The `auth:` section refers to
environment variables (`env:LINKKEEP_AUTH`), so nothing secret is in the file: an unset
variable drops that header and prints a warning naming the variable.

## Using the workflow

```bash
mkdir -p .github/workflows
cp examples/github-actions-linkcheck.yml .github/workflows/linkcheck.yml
# edit SITE (and add a schedule you actually want), then:
gh workflow run linkcheck.yml       # try it now instead of waiting for Monday
```

The important three lines of it:

```yaml
- run: linkkeep --db "$DB" crawl "$SITE" --fail-on never --format json --output report.json
- run: linkkeep --db "$DB" diff --format markdown >>"$GITHUB_STEP_SUMMARY"
```

`crawl --fail-on never` records the run without deciding the job's outcome; `diff` exits
`1` only if findings are **new**, so the pipeline fails on regressions you caused rather
than on problems the team already knows about. Remove `--fail-on never` if you would rather
fail on any current error.

## Watching instead of scheduling

`linkkeep watch` is the same crawl on an interval, diffing against the previous stored run,
with an optional webhook POST when new issues appear (payload documented in the
[README](../README.md#webhook-payload)):

```bash
linkkeep watch https://example.com --every 6h --notify-webhook "$SLACK_URL"
linkkeep watch https://example.com --once          # single iteration, for cron/CI
```
