#!/usr/bin/env python3
"""Regenerate the golden report files under ``tests/goldens``.

    make goldens
    # or
    python tests/update_goldens.py

Review the resulting diff before committing: a change here means a report format changed,
which is a user-visible change and needs a note in ``CHANGELOG.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow running without installing the package
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))

from linkkeep.report import render  # noqa: E402
from tests.report_samples import sample_report_data  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "goldens"

#: golden filename -> (report format, include the diff block?)
TARGETS: dict[str, tuple[str, bool]] = {
    "report_text.txt": ("text", False),
    "report_text_diff.txt": ("text", True),
    "report_markdown.md": ("markdown", False),
    "report_markdown_diff.md": ("markdown", True),
    "report_html.html": ("html", False),
    "report_json.json": ("json", False),
    "report_json_diff.json": ("json", True),
    "report_junit.xml": ("junit", False),
}


def write_goldens(directory: Path = GOLDEN_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, (fmt, with_diff) in TARGETS.items():
        text = render(sample_report_data(with_diff=with_diff), fmt)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    for path in write_goldens():
        print(
            f"wrote {path.relative_to(ROOT)}  ({len(path.read_text(encoding='utf-8').splitlines())} lines)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
