#!/usr/bin/env python3
"""Take Linkkeep for a real drive: crawl a local demo site, break it, fix it, diff it.

    make demo                 # or: python tools/demo.py
    make demo DEMO_ARGS=--save

No network access is needed: the site is :mod:`linkkeep.fixture_site`, served from
``127.0.0.1`` on a random port.  Every command below is the real CLI, with the real
fetcher, the real SQLite file and the real report renderers - which makes this the
script to run before you believe a README example.

``--save`` writes everything it printed to ``demo-output/`` so the output can be copied
into documentation (that directory is git-ignored).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import socket
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from linkkeep import cli  # noqa: E402
from linkkeep.fixture_site import running_site  # noqa: E402

RULE = "=" * 78


class Step:
    """Runs one CLI invocation, echoes it, and keeps the transcript for ``--save``."""

    def __init__(self, global_args: Sequence[str], recording: Path | None) -> None:
        self.global_args = list(global_args)
        self.recording = recording
        self.failures = 0

    def __call__(
        self, argv: Sequence[str], *, save_as: str = "", expect: int | tuple[int, ...] | None = None
    ) -> str:
        full = [*self.global_args, *argv]
        printable = "linkkeep " + " ".join(full)
        print(f"\n{RULE}\n$ {printable}\n{RULE}")
        buffer = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                cli.main(list(full))
            except SystemExit as exc:
                code = int(exc.code or 0)
        text = buffer.getvalue().rstrip("\n")
        print(text)
        print(f"[exit code: {code}]")
        if expect is not None and code not in (expect if isinstance(expect, tuple) else (expect,)):
            print("!! unexpected exit code")
            self.failures += 1
        if save_as and self.recording is not None:
            (self.recording / save_as).write_text(
                f"$ {printable}\n{text}\n[exit code: {code}]\n", encoding="utf-8"
            )
        return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Linkkeep against the bundled demo site.")
    parser.add_argument(
        "--save", action="store_true", help="write transcripts and reports to --outdir"
    )
    parser.add_argument(
        "--outdir", default=str(ROOT / "demo-output"), type=Path, help="where --save writes"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8077,
        help="port for the demo site (a random free one if it is busy)",
    )
    args = parser.parse_args(argv)

    # rich renders at 80 columns when stdout is not a terminal; the docs want more room
    os.environ.setdefault("COLUMNS", "100")
    os.environ.pop("FORCE_COLOR", None)

    outdir: Path | None = None
    if args.save:
        outdir = args.outdir
        outdir.mkdir(parents=True, exist_ok=True)
    workdir = outdir or Path(tempfile.mkdtemp(prefix="linkkeep-demo-"))
    db = workdir / "demo.sqlite"
    if db.exists():
        db.unlink()

    step = Step(["--db", str(db)], outdir)
    crawl_flags = ["--allow-private", "--check-fragments", "--sitemap", "--slow-ms", "300"]

    with running_site(variant="broken", port=pick_port(args.port)) as server:
        site = server.url + "/"
        print(RULE)
        print(f"demo site:   {site}")
        print(f"history db:  {db}")
        print("the site is planted with broken links, a redirect loop, a 500, a 410, a")
        print("robots-only area, an unreachable partner domain and one slow page")
        print(RULE)

        step(["init", "--path", str(workdir / "linkkeep.yaml")])
        step(["crawl", site, *crawl_flags], save_as="01-crawl.txt", expect=(0, 1))
        step(["runs"], save_as="02-runs.txt", expect=0)
        step(["crawl", site, *crawl_flags, "--format", "none"], save_as="03-recrawl.txt")
        step(["runs"], save_as="04-runs-again.txt")
        step(["diff"], save_as="05-diff-same-content.txt")

        print(f"\n{RULE}\n# now someone fixes the site (same server, different content)\n{RULE}")
        server.set_variant("fixed")
        step(["crawl", site, *crawl_flags, "--format", "none"])
        step(["diff", "-q"], save_as="06-diff-quiet.txt", expect=0)
        step(["diff"], save_as="07-diff-after-fix.txt", expect=0)

        newest = _newest_run(db)
        for fmt in ("markdown", "html", "junit", "json", "text"):
            target = workdir / f"report.{fmt}"
            step(["report", str(newest), "--format", fmt, "--output", str(target)])
            if outdir is not None:
                (outdir / f"08-report.{fmt}").write_text(
                    target.read_text(encoding="utf-8"), encoding="utf-8"
                )
        _check_junit(workdir / "report.junit")

        step(
            ["watch", site, "--once", "--every", "6h", "-q", "--allow-private"],
            save_as="09-watch.txt",
        )
        step(["prune", "--older-than", "90d", "--yes"], save_as="10-prune.txt")

    print(f"\n{RULE}\nreports{'  (saved)' if outdir else ' were written to'} {workdir}\n{RULE}")
    for report in sorted(workdir.glob("report.*")):
        print(f"  {report.name:<20} {report.stat().st_size:>7,} bytes")
    if step.failures:  # pragma: no cover - the demo doubles as a smoke test
        print(f"!! {step.failures} step(s) returned an unexpected exit code")
    return 1 if step.failures else 0


def pick_port(preferred: int) -> int:
    """Bind ``preferred`` if it is free, so the printed URLs match the documented ones."""
    with closing(socket.socket()) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            return 0
    return preferred


def _newest_run(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    return int(row[0]) if row and row[0] is not None else 1


def _check_junit(path: Path) -> None:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    counts: dict[str, str | None] = {
        key: root.attrib.get(key) for key in ("tests", "failures", "errors", "skipped")
    }
    print(f"\njunit.xml parses as XML: {counts}")
    if int(counts["failures"] or 0) <= 0:  # pragma: no cover - demo invariant
        raise AssertionError("the demo site must produce failures")


if __name__ == "__main__":
    raise SystemExit(main())
