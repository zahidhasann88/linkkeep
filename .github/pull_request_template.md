## What does this change?

<!-- One or two sentences. Link the issue it closes: `Closes #123`. -->

## Why now?

<!-- The user-visible problem, not the implementation. If this fixes a broken link check,
     paste the output that made it obvious. -->

## How was it tested?

- [ ] `make check` passes (ruff, `ruff format --check`, mypy strict, pytest with the 85% gate)
- [ ] added or changed a test for the behaviour in this PR
- [ ] no test touches the real internet (unit tests stub the fetcher or use respx; the
      integration tests bind `127.0.0.1`)
- [ ] report output changes were regenerated with `make goldens` and reviewed as a diff
- [ ] `make demo` still works (`python tools/demo.py`)
- [ ] README/docs updated (issue table, config reference, or `examples/`) where relevant

## Notes for the reviewer

<!-- Anything surprising: a deliberate deviation, an edge case left unfixed, a behaviour
     change in exit codes or in the JSON schema (schema changes need a version bump). -->
