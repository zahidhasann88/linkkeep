"""JUnit XML report so CI systems can display results per test case.

Shape (one test case per issue, grouped into a test suite per source page):

* a failing status (4xx/5xx, unreachable, loop) becomes ``<failure>``
* a warning becomes ``<failure>`` too, because CI only has pass/fail/skip - the
  ``severity`` is kept in the ``message`` attribute and as a property
* info-only findings become ``<skipped>`` so they are visible but never red

The run itself is reported in ``<properties>`` (pages crawled, links checked, ...).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any

from linkkeep.models import Issue, Severity
from linkkeep.report import ReportData, chain_summary, describe_issue, iso

#: Control codes XML 1.0 cannot represent (tab, LF and CR are the legal ones).
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def clean(value: str) -> str:
    """Drop characters that would make the document unparseable.

    Link text and titles come from fetched HTML, and a hostile or merely broken page can
    put a raw control code in one.  ElementTree passes such characters through, and the
    CI system then fails to parse the file at all - which is worse than a missing letter.
    """
    return _ILLEGAL_XML.sub("", value)


SEVERITY_TO_RESULT = {
    Severity.ERROR: "failure",
    Severity.WARNING: "failure",
    Severity.INFO: "skipped",
}


def build_tree(data: ReportData) -> ET.Element:
    run = data.run
    counts = data.counts_by_severity
    suites: dict[str, list[Issue]] = defaultdict(list)
    for issue in data.sorted_issues:
        suites[issue.source_url or run.site].append(issue)
    if not suites:
        suites[run.site] = []

    testsuites = ET.Element(
        "testsuites",
        _attrs(
            {
                "name": f"linkkeep {run.site}",
                "tests": str(max(1, len(data.issues))),
                "failures": str(counts[Severity.ERROR] + counts[Severity.WARNING]),
                "skipped": str(counts[Severity.INFO] if data.issues else 1),
                "errors": "0",
                "time": f"{data.duration_seconds:.3f}",
                "timestamp": iso(run.started_at),
            }
        ),
    )
    for index, (source_url, issues) in enumerate(sorted(suites.items())):
        failures = sum(1 for issue in issues if SEVERITY_TO_RESULT[issue.severity] == "failure")
        skipped = sum(1 for issue in issues if SEVERITY_TO_RESULT[issue.severity] == "skipped")
        suite = ET.SubElement(
            testsuites,
            "testsuite",
            _attrs(
                {
                    "name": source_url,
                    "tests": str(max(1, len(issues))),
                    "failures": str(failures),
                    "skipped": str(skipped if issues else 1),
                    "errors": "0",
                    "id": str(index),
                    "package": run.site,
                }
            ),
        )
        properties = ET.SubElement(suite, "properties")
        _prop(properties, "linkkeep.run_id", str(run.id))
        _prop(properties, "linkkeep.status", run.status.value)
        _prop(properties, "linkkeep.pages", str(run.counts.pages))
        _prop(properties, "linkkeep.links_checked", str(run.counts.checked))
        if not issues:
            case = ET.SubElement(
                suite,
                "testcase",
                _attrs({"classname": "linkkeep", "name": "no issues on this page", "time": "0"}),
            )
            ET.SubElement(case, "skipped", {"message": "0 issues"})
            continue
        for issue in issues:
            case = ET.SubElement(
                suite,
                "testcase",
                _attrs(
                    {
                        "classname": f"linkkeep.{issue.code}",
                        "name": issue.target_url,
                        "time": "0",
                        "status": SEVERITY_TO_RESULT[issue.severity],
                    }
                ),
            )
            message = f"{issue.severity.value}: {issue.code} - {describe_issue(issue)}"
            body = "\n".join(
                part
                for part in (
                    f"target:  {issue.target_url}",
                    f"source:  {issue.source_url or '-'}",
                    f"final:   {issue.final_url or '-'}",
                    f"element: {issue.element or '-'} {issue.attribute or ''}".strip(),
                    f"text:    {issue.link_text}" if issue.link_text else "",
                    f"reason:  {issue.reason}" if issue.reason else "",
                    chain_summary(issue) or "",
                    f"fingerprint: {issue.fingerprint}",
                )
                if part
            )
            tag = SEVERITY_TO_RESULT[issue.severity]
            detail = ET.SubElement(case, tag, _attrs({"message": message, "type": issue.code}))
            detail.text = clean(body)
    return testsuites


def render(data: ReportData) -> str:
    return to_xml(build_tree(data))


def to_xml(root: ET.Element) -> str:
    _indent(root)
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


def _attrs(values: dict[str, str]) -> dict[str, str]:
    """Escape-hatch-free sanitisation for every attribute we build from user data."""
    return {name: clean(value) for name, value in values.items()}


def _prop(parent: ET.Element, name: str, value: str) -> None:
    ET.SubElement(parent, "property", _attrs({"name": name, "value": value}))


def _indent(element: ET.Element, level: int = 0) -> None:
    """Pretty-print helper (``ET.indent`` is 3.9+, we support 3.10 so it exists)."""
    ET.indent(element, space="  ")


def validate(text: str) -> dict[str, Any]:
    """Parse the XML back and return a couple of counters (used by tests and CI)."""
    root = ET.fromstring(text)
    return {
        "tests": int(root.attrib.get("tests", "0")),
        "failures": int(root.attrib.get("failures", "0")),
        "skipped": int(root.attrib.get("skipped", "0")),
        "suites": len(root.findall("testsuite")),
        "cases": len(root.findall(".//testcase")),
    }
