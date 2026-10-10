"""Report renderers: golden files, structure and safety properties."""

from __future__ import annotations

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from tests.report_samples import ISSUES, LINKS, PAGES, STARTED, sample_report_data

from linkkeep.models import RunStatus
from linkkeep.report import (
    JSON_SCHEMA_VERSION,
    RENDERERS,
    age_label,
    describe_issue,
    from_crawl,
    render,
    shorten,
    stamp,
    where_seen,
    write_output,
)
from linkkeep.report import html as html_report
from linkkeep.report import junit as junit_report
from linkkeep.report import markdown as markdown_report
from linkkeep.report import xjson as json_report

GOLDENS = Path(__file__).parent / "goldens"


def golden(name: str) -> str:
    return (GOLDENS / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------- golden files
@pytest.mark.parametrize(
    ("fmt", "filename"),
    [
        ("text", "report_text.txt"),
        ("markdown", "report_markdown.md"),
        ("html", "report_html.html"),
        ("json", "report_json.json"),
        ("junit", "report_junit.xml"),
    ],
)
def test_golden_files_match(fmt: str, filename: str) -> None:
    assert render(sample_report_data(), fmt) == golden(filename)


def test_golden_text_report_with_diff() -> None:
    assert render(sample_report_data(with_diff=True), "text") == golden("report_text_diff.txt")


def test_golden_markdown_report_with_diff() -> None:
    assert render(sample_report_data(with_diff=True), "markdown") == golden(
        "report_markdown_diff.md"
    )


def test_golden_json_report_with_diff() -> None:
    assert render(sample_report_data(with_diff=True), "json") == golden("report_json_diff.json")


def test_goldens_exist_for_every_format() -> None:
    present = {path.name for path in GOLDENS.glob("*")}
    assert {
        "report_text.txt",
        "report_markdown.md",
        "report_html.html",
        "report_json.json",
        "report_junit.xml",
    } <= present


# ------------------------------------------------------------------------ text
def test_text_report_is_grouped_by_type_then_source() -> None:
    text = render(sample_report_data(), "text")
    assert text.index("[ERROR] broken") < text.index("[WARNING] mixed_content")
    assert text.index("[INFO] slow") > text.index("[WARNING] mixed_content")
    # grouped by source page inside each type
    block = text.split("[ERROR] broken")[1].split("[ERROR] redirect_loop")[0]
    sources = [line for line in block.splitlines() if line.strip().startswith("from:")]
    assert sources == [
        "  from: https://example.com/",
        "  from: https://example.com/blog/post-1",
    ]
    assert "docs/handbook.pdf" in block


def test_text_report_shows_counts_and_a_summary() -> None:
    text = render(sample_report_data(), "text")
    assert "Linkkeep 0.1.0 - run #42 - https://example.com" in text
    assert "5 errors, 4 warnings, 3 info" in text
    assert "pages      6 crawled (1 blocked by robots)" in text
    assert "by type    blocked_by_robots=1, broken=2" in text
    assert "duration   12.3s" in text
    assert "sitemap added 3 URL(s)" in text


def test_text_report_renders_redirect_chains() -> None:
    text = render(sample_report_data(), "text")
    assert "chain:" in text
    assert "301 https://example.com/old/news" in text


def test_clean_run_says_so() -> None:
    data = sample_report_data()
    data.issues = []
    text = render(data, "text")
    assert "No issues found." in text
    assert "ISSUES" not in text


def test_interrupted_run_is_visible() -> None:
    data = sample_report_data()
    data.run = data.run.model_copy(update={"status": RunStatus.INTERRUPTED})
    text = render(data, "text")
    assert "status     interrupted" in text
    assert "Linkkeep 0.1.0 - run #42 - https://example.com" in text


def test_text_helper_formatting() -> None:
    assert stamp(None) == "-"
    assert stamp(ISSUES[0].found_at).startswith("2026-03-04 09:00")
    assert shorten("x" * 200, 20).endswith("\u2026")
    assert describe_issue(ISSUES[0]) == "HTTP 404"
    assert where_seen(ISSUES[8]) == '<img src> "Banner"'
    assert age_label(STARTED, now=STARTED + dt.timedelta(days=3)) == "3 days"
    assert age_label(STARTED, now=STARTED + dt.timedelta(hours=5)) == "today"
    assert age_label(STARTED, now=STARTED + dt.timedelta(days=1)) == "1 day"
    assert age_label(STARTED, now=STARTED + dt.timedelta(days=21)) == "3 weeks"
    assert age_label(STARTED, now=STARTED + dt.timedelta(days=200)) == "6 months"
    assert age_label(STARTED, now=STARTED - dt.timedelta(days=4)) == "today"


# -------------------------------------------------------------------- markdown
def test_markdown_report_is_valid_github_flavoured_tables() -> None:
    text = render(sample_report_data(), "markdown")
    assert text.startswith("## Linkkeep: https://example.com")
    assert "| severity | count | pages | links checked | robots-blocked |" in text
    assert "| :x: errors | 5 | 6 | 41 | 1 |" in text
    assert "| :warning: warnings | 4 | | | |" in text
    rows = [line for line in text.splitlines() if line.startswith("| :") and "| `" in line]
    assert len(rows) == len(ISSUES)
    for row in rows:
        assert row.count("|") == 6, row
    assert (
        "| :x: | `broken` | [`https://example.com/shop/checkout`](https://example.com/shop/checkout) | 500 |"
        in text
    )


def test_markdown_uses_details_blocks_and_emojis() -> None:
    text = render(sample_report_data(), "markdown")
    assert "<details><summary>" in text
    assert ":robot:" in text
    assert ":turtle:" in text
    assert text.count("</details>") == text.count("<details>")


def test_markdown_diff_section_uses_callouts() -> None:
    text = render(sample_report_data(with_diff=True), "markdown")
    assert "### Changes since the previous crawl" in text
    assert ":rotating_light:" in text
    assert ":checkered_flag:" in text
    assert "broken since" in text


def test_markdown_clean_run() -> None:
    data = sample_report_data()
    data.issues = []
    text = render(data, "markdown")
    assert "No issues found." in text
    assert "| `broken` |" not in text


def test_markdown_link_helper() -> None:
    assert markdown_report.md_link(None) == "-"
    assert markdown_report.md_link("https://a.test/x") == "[`https://a.test/x`](https://a.test/x)"


# ------------------------------------------------------------------------ html
def test_html_report_is_self_contained() -> None:
    text = render(sample_report_data(), "html")
    assert text.startswith("<!doctype html>")
    assert "<style>" in text
    assert "</style>" in text
    assert "<script>" in text
    # no external assets of any kind
    assert not re.search(r'<link[^>]+href="http', text), "no external stylesheets"
    assert not re.search(r"<script[^>]+src=", text), "no external scripts"
    assert "url(" not in text
    assert "@import" not in text
    assert 'name="viewport"' in text


def test_html_report_is_filterable() -> None:
    text = render(sample_report_data(), "html")
    for issue in ISSUES:
        assert f'data-type="{issue.code}"' in text
    for severity in ("error", "warning", "info"):
        assert f'data-severity="{severity}"' in text
        assert f'data-value="{severity}"' in text
    assert 'data-filter="type"' in text
    assert "getElementById('q')" in text or 'id="q"' in text
    assert "querySelectorAll" in text


def test_html_report_escapes_untrusted_content() -> None:
    data = sample_report_data()
    nasty = '<img src=x onerror="alert(1)">'
    data.issues = [
        data.issues[0].model_copy(update={"link_text": nasty, "reason": "</script><b>x</b>"})
    ]
    text = render(data, "html")
    assert nasty not in text
    assert "&lt;img src=x" in text
    assert "</script><b>" not in text


def test_markdown_and_text_keep_raw_link_text() -> None:
    data = sample_report_data()
    data.issues = [data.issues[0].model_copy(update={"link_text": 'Say "hi" <b>now</b>'})]
    text = render(data, "text")
    assert '"Say "hi" <b>now</b>"' in text
    assert "<b>now</b>" in render(data, "markdown")


def test_html_report_handles_a_clean_run() -> None:
    data = sample_report_data()
    data.issues = []
    text = render(data, "html")
    assert "No issues found" in text
    assert "<tbody>" not in text


def test_html_report_includes_the_diff_panels() -> None:
    text = render(sample_report_data(with_diff=True), "html")
    assert "New (" in text
    assert "Fixed (" in text
    assert "Persisting (" in text
    assert "Changes since run #41" in text


def test_html_css_has_a_mobile_breakpoint() -> None:
    assert "@media (max-width:720px)" in html_report.CSS


# -------------------------------------------------------------------------- json
def test_json_report_schema_is_stable() -> None:
    data = sample_report_data()
    payload = json.loads(render(data, "json"))
    assert sorted(payload) == sorted(
        [
            "schema_version",
            "tool",
            "generated_at",
            "site",
            "run",
            "counts",
            "issues",
            "diff",
            "pages",
            "links",
        ]
    )
    assert payload["schema_version"] == JSON_SCHEMA_VERSION
    assert payload["tool"] == {"name": "linkkeep", "version": "0.1.0"}
    assert payload["site"] == "https://example.com"
    assert sorted(payload["run"]) == sorted(
        [
            "id",
            "site",
            "status",
            "started_at",
            "finished_at",
            "duration_seconds",
            "seeds",
            "notes",
            "config",
        ]
    )
    assert payload["run"]["started_at"] == "2026-03-04T09:00:00+00:00"
    assert {
        k: payload["counts"][k] for k in ("errors", "warnings", "infos", "pages", "checked")
    } == {
        "errors": 5,
        "warnings": 4,
        "infos": 3,
        "pages": 6,
        "checked": 41,
    }
    assert len(payload["issues"]) == len(ISSUES)
    assert payload["diff"] is None
    assert len(payload["pages"]) == len(PAGES)
    assert len(payload["links"]) == len(LINKS)


def test_json_issue_object_shape() -> None:
    payload = json.loads(render(sample_report_data(), "json"))
    issue = payload["issues"][0]  # sorted: severity, then type name, then URL
    assert sorted(issue) == sorted(
        [
            "code",
            "severity",
            "fingerprint",
            "source_url",
            "target_url",
            "final_url",
            "status",
            "reason",
            "element",
            "attribute",
            "link_text",
            "redirect_chain",
            "found_at",
            "description",
            "summary",
        ]
    )
    assert issue["code"] == "broken"
    assert issue["severity"] == "error"
    assert issue["source_url"] == "https://example.com/"
    assert issue["target_url"] == "https://example.com/shop/checkout"
    assert issue["status"] == 500
    assert issue["found_at"] == "2026-03-04T09:00:02+00:00"
    assert issue["redirect_chain"] == []
    assert issue["description"] == "Target responded with a 4xx/5xx status"
    assert issue["summary"].startswith("HTTP 500")
    chain_issue = next(item for item in payload["issues"] if item["code"] == "redirect_chain")
    assert chain_issue["redirect_chain"][0] == {
        "url": "https://example.com/old/news",
        "status": 301,
        "location": "https://example.com/n1",
    }
    assert chain_issue["description"] == "Too many redirects in the chain"


def test_json_diff_block() -> None:
    payload = json.loads(render(sample_report_data(with_diff=True), "json"))
    diff = payload["diff"]
    assert sorted(diff) == sorted(["run_a", "run_b", "new", "fixed", "persisting", "notes"])
    assert diff["run_a"]["id"] == 41
    assert all(
        "since" in item and "runs_seen" in item and "issue" in item for item in diff["persisting"]
    )


def test_json_is_utf8_and_indented() -> None:
    text = render(sample_report_data(), "json")
    assert text.endswith("\n")
    assert '\n  "schema_version"' in text
    assert "example.com" in text


# ----------------------------------------------------------------------- junit
def test_junit_xml_parses_and_counts() -> None:
    text = render(sample_report_data(), "junit")
    root = ET.fromstring(text)
    assert root.tag == "testsuites"
    assert int(root.attrib["tests"]) == len(ISSUES)
    assert int(root.attrib["failures"]) == 9  # errors + warnings
    assert int(root.attrib["skipped"]) == 3  # info-level findings
    cases = root.findall(".//testcase")
    assert len(cases) == len(ISSUES)
    assert {case.attrib["classname"].split(".")[1] for case in cases} == {
        issue.code for issue in ISSUES
    }


def test_junit_strips_characters_xml_cannot_represent() -> None:
    """A hostile page can put control codes in link text; the file must still parse."""
    data = sample_report_data()
    nasty = data.issues[0].model_copy(
        update={
            "link_text": "click\u0000here\u000b",
            "reason": "bad\u001fchar",
            "source_url": "https://example.com/\u0001page",
        }
    )
    data.issues = [nasty, *data.issues[1:]]

    text = render(data, "junit")
    assert ET.fromstring(text).tag == "testsuites"  # would raise if the XML were invalid
    assert "clickhere" in text
    assert "badchar" in text
    assert "\u0000" not in text
    assert "\u000b" not in text


def test_junit_details_contain_urls_and_fingerprints() -> None:
    root = ET.fromstring(render(sample_report_data(), "junit"))
    texts = [
        case.find("failure").text
        for case in root.findall(".//testcase")
        if case.find("failure") is not None
    ]
    joined = "\n".join(part for part in texts if part)
    assert "https://example.com/docs/handbook.pdf" in joined
    assert "fingerprint: a1b2c3d4e5f60708" in joined
    assert "target:" in joined
    assert "source:" in joined


def test_junit_properties_carry_run_metadata() -> None:
    root = ET.fromstring(render(sample_report_data(), "junit"))
    props = {prop.attrib["name"]: prop.attrib["value"] for prop in root.findall(".//property")}
    assert props["linkkeep.run_id"] == "42"
    assert props["linkkeep.status"] == "completed"
    assert props["linkkeep.pages"] == "6"


def test_junit_clean_run_has_one_passing_case_per_suite() -> None:
    data = sample_report_data()
    data.issues = []
    root = ET.fromstring(render(data, "junit"))
    assert root.attrib["tests"] == "0"
    cases = root.findall(".//testcase")
    assert len(cases) == 1
    assert cases[0].find("skipped") is not None


def test_junit_validate_helper() -> None:
    stats = junit_report.validate(render(sample_report_data(), "junit"))
    assert stats["tests"] == len(ISSUES)
    assert stats["suites"] >= 1
    assert stats["cases"] == len(ISSUES)


# ------------------------------------------------------------------- factories
def test_from_crawl_accepts_a_crawler_report() -> None:
    report = CrawlLikeReport()
    data = from_crawl(report)
    assert data.run.site == "https://example.com"
    assert len(data.issues) == 1
    assert data.diff is None


class CrawlLikeReport:
    """Duck-typed stand-in for :class:`linkkeep.crawler.CrawlReport`."""

    site = "https://example.com"
    seeds = ["https://example.com/"]
    issues = [ISSUES[0]]
    pages = PAGES[:1]
    links = LINKS[:1]
    counts = sample_report_data().run.counts
    notes: list[str] = []
    config: dict[str, object] = {}
    status_value = "completed"

    def to_run_record(self) -> object:
        return sample_report_data().run


def test_render_rejects_unknown_formats() -> None:
    with pytest.raises(ValueError, match="unknown report format"):
        render(sample_report_data(), "pdf")


def test_every_registered_format_renders_a_string() -> None:
    for name, renderer in RENDERERS.items():
        text = renderer(sample_report_data())
        assert isinstance(text, str)
        assert len(text) > 50, name


def test_write_output_to_file_and_stdout(tmp_path: Path) -> None:
    seen: list[str] = []
    assert write_output("hello", None, console_print=seen.append) is False
    assert seen == ["hello"]
    target = tmp_path / "nested" / "report.txt"
    assert write_output("hello", target, console_print=seen.append) is True
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert seen[-1].startswith("wrote ")


def test_write_output_dash_means_stdout() -> None:
    seen: list[str] = []
    assert write_output("body", "-", console_print=seen.append) is False
    assert seen == ["body"]


def test_diff_ages_are_pinned_to_the_report_timestamp() -> None:
    """Ages must be derived from the report, never the wall clock, or goldens go stale."""
    data = sample_report_data(with_diff=True)
    assert "broken since 2026-02-25 (7 days)" in render(data, "text")
    assert "broken since 2026-02-25 (7 days), seen in 4 runs" in render(data, "markdown")
    assert "broken since 2026-02-25 (7 days)" in render(data, "html")
    later = replace(data, generated_at=data.generated_at + dt.timedelta(days=6))
    assert "broken since 2026-02-25 (13 days)" in render(later, "text")
    assert "broken since 2026-02-25 (7 days)" not in render(later, "text")
    much_later = replace(data, generated_at=data.generated_at + dt.timedelta(days=7))
    assert "broken since 2026-02-25 (2 weeks)" in render(much_later, "text")


def test_json_helper_renders_pages_and_links_sections() -> None:
    payload = json_report.to_dict(sample_report_data(), include_details=False)
    assert "pages" not in payload
    assert "links" not in payload
    assert "issues" in payload


def test_renderers_all_deal_with_unicode_urls() -> None:
    data = sample_report_data()
    data.issues = [
        ISSUES[0].model_copy(
            update={"target_url": "https://example.com/bücher/ß", "link_text": "Bücher"}
        )
    ]
    for fmt in ("text", "markdown", "html", "json"):
        text = render(data, fmt)
        assert "bücher" in text or "b%C3%BCcher" in text, fmt
