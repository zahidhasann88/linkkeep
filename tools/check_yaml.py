#!/usr/bin/env python3
"""Parse every YAML file in the repository and validate the example config.

Small on purpose: it runs in CI before the test suite, and it catches the two mistakes
that are easy to make in files like these - invalid YAML, and an example config that the
tool itself would reject.

    python tools/check_yaml.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
GLOBS = ("examples/*.y*ml", ".github/**/*.y*ml", ".pre-commit-config.yaml")


def main() -> int:
    files: list[Path] = []
    for pattern in GLOBS:
        files.extend(sorted(REPO.glob(pattern)))

    failures = 0
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # report every parse failure, whatever it is
            print(f"FAIL {path.relative_to(REPO)}: {exc}")
            failures += 1
            continue
        kind = type(data).__name__
        print(f"ok   {path.relative_to(REPO)} ({kind})")

    # the example config must be a config Linkkeep itself would accept
    try:
        from linkkeep.config import INIT_CONFIG_YAML, Config

        Config.from_dict(yaml.safe_load(REPO.joinpath("examples/linkkeep.yaml").read_text("utf-8")))
        Config.from_dict(yaml.safe_load(INIT_CONFIG_YAML))
        print("ok   examples/linkkeep.yaml + `linkkeep init` config load into Config")
    except Exception as exc:  # an unusable example is a docs bug, not a crash
        print(f"FAIL example config: {exc}")
        failures += 1

    if failures:
        print(f"{failures} file(s) failed")
        return 1
    print(f"{len(files)} YAML file(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
