"""Link extraction from messy markup."""

from __future__ import annotations

from pathlib import Path

import pytest

from linkkeep.extract import (
    ELEMENT_ATTRIBUTES,
    RESOURCE_ELEMENTS,
    collect_anchors,
    extract_links,
    fragment_is_valid,
    parse_srcset,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://site.test/blog/post.html"


def urls(result: object) -> list[str]:
    return [link.url for link in result.links]  # type: ignore[attr-defined]


def test_element_attributes_are_the_documented_set() -> None:
    assert dict(ELEMENT_ATTRIBUTES) == {
        "a": "href",
        "link": "href",
        "img": "src",
        "script": "src",
        "iframe": "src",
        "source": "src",
    }
    assert {"img", "script", "source", "iframe", "link"} == RESOURCE_ELEMENTS


def test_simple_extraction() -> None:
    html = '<a href="/a">A</a><img src="/b.png"><script src="/c.js"></script>'
    result = extract_links(html, BASE)
    assert urls(result) == [
        "https://site.test/a",
        "https://site.test/b.png",
        "https://site.test/c.js",
    ]
    assert [link.element for link in result.links] == ["a", "img", "script"]
    assert [link.attribute for link in result.links] == ["href", "src", "src"]


def test_relative_paths_and_dot_segments() -> None:
    html = '<a href="x/y.html">x</a><a href="../up.html">up</a><a href="./here.html">here</a>'
    result = extract_links(html, "https://site.test/a/b/c.html")
    assert set(urls(result)) == {
        "https://site.test/a/b/x/y.html",
        "https://site.test/a/up.html",
        "https://site.test/a/b/here.html",
    }


def test_base_href_wins_over_page_url() -> None:
    html = '<html><head><base href="/assets/"></head><body><a href="one.html">1</a></body></html>'
    result = extract_links(html, BASE)
    assert urls(result) == ["https://site.test/assets/one.html"]
    assert result.base_href == "/assets/"


def test_base_href_absolute_url() -> None:
    html = '<base href="https://cdn.test/v2/"><a href="x.css">x</a>'
    result = extract_links(html, BASE)
    assert urls(result) == ["https://cdn.test/v2/x.css"]


def test_fragments_are_split_off_but_kept() -> None:
    html = '<a href="/page#intro">i</a><a href="#top">t</a><a href="/other.html#x?y=1">o</a>'
    result = extract_links(html, BASE)
    assert [(link.url, link.fragment) for link in result.links] == [
        ("https://site.test/page", "intro"),
        ("https://site.test/other.html", "x?y=1"),
    ]
    # a same-page "#..." link is not worth checking, but it is counted in the summary
    assert result.skipped["fragment-only"] == 1


def test_non_http_schemes_are_counted_not_followed() -> None:
    html = (
        '<a href="mailto:a@b.test">m</a><a href="tel:123">t</a>'
        '<a href="javascript:void(0)">j</a><a href="data:text/plain,x">d</a><a href="/ok">ok</a>'
    )
    result = extract_links(html, BASE)
    assert urls(result) == ["https://site.test/ok"]
    assert result.skipped["scheme"] == 4
    assert result.skipped_schemes == {"mailto": 1, "tel": 1, "javascript": 1, "data": 1}


def test_srcset_urls_are_extracted() -> None:
    html = '<img src="a.png" srcset="b.png 1x, c.png 2x"><source srcset="d.avif 800w">'
    result = extract_links(html, "https://site.test/img/")
    assert set(urls(result)) == {
        "https://site.test/img/a.png",
        "https://site.test/img/b.png",
        "https://site.test/img/c.png",
        "https://site.test/img/d.avif",
    }
    assert [link.attribute for link in result.links].count("srcset") == 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a.png 1x, b.png 2x", ["a.png", "b.png"]),
        ("a.png", ["a.png"]),
        ("  a.png   300w ,b.png  ", ["a.png", "b.png"]),
        ("", []),
        ("<a.png> 1x, <b.png>", ["<a.png>", "<b.png>"]),
    ],
)
def test_parse_srcset(value: str, expected: list[str]) -> None:
    assert parse_srcset(value) == expected


def test_duplicate_urls_are_deduplicated() -> None:
    html = '<a href="/x">1</a><a href="/x">2</a><img src="/x">'
    result = extract_links(html, BASE)
    assert urls(result) == ["https://site.test/x", "https://site.test/x"]
    assert {link.element for link in result.links} == {"a", "img"}


def test_link_text_is_cleaned_and_truncated() -> None:
    html = '<a href="/a">  lots \n of\t whitespace  </a>'
    result = extract_links(html, BASE)
    assert result.links[0].link_text == "lots of whitespace"
    long = '<a href="/b">' + "x" * 400 + "</a>"
    assert len(extract_links(long, BASE).links[0].link_text or "") <= 200


def test_img_alt_is_used_as_label() -> None:
    result = extract_links('<img src="/a.png" alt="The logo">', BASE)
    assert result.links[0].link_text == "The logo"


def test_protocol_relative_and_ports_and_idn() -> None:
    html = (
        '<a href="//cdn.test/x">p</a><a href="https://site.test:443/y">d</a>'
        '<a href="https://Bücher.example/z">i</a>'
    )
    assert urls(extract_links(html, BASE)) == [
        "https://cdn.test/x",
        "https://site.test/y",
        "https://xn--bcher-kva.example/z",
    ]


def test_query_parameters_sorted_when_asked() -> None:
    html = '<a href="/s?b=2&a=1">q</a>'
    assert urls(extract_links(html, BASE)) == ["https://site.test/s?b=2&a=1"]
    assert urls(extract_links(html, BASE, sort_query=True)) == ["https://site.test/s?a=1&b=2"]


def test_trailing_slash_policy_applies_to_extraction() -> None:
    html = '<a href="/dir/">d</a>'
    assert urls(extract_links(html, BASE, slash_policy="strip")) == ["https://site.test/dir"]


def test_anchors_are_collected() -> None:
    html = '<div id="a"></div><a name="b"></a><p id="c">x</p><span>no id</span>'
    assert collect_anchors(html) == {"a", "b", "c"}


@pytest.mark.parametrize(
    ("fragment", "valid"),
    [
        ("intro", True),
        ("a-b_c", True),
        ("top1", True),
        ("", False),
        ("a/b", False),
        ("[id=x]", False),
        ("1.2.3", False),
    ],
)
def test_fragment_is_valid(fragment: str, valid: bool) -> None:
    assert fragment_is_valid(fragment) is valid


def test_messy_fixture_file() -> None:
    html = (FIXTURES / "messy.html").read_text(encoding="utf-8")
    result = extract_links(html, "https://site.test/blog/post.html")
    found = set(urls(result))
    # base href="/deep/nested/" applies to every relative reference
    assert "https://site.test/deep/nested/one.html" in found
    assert "https://site.test/deep/nested/two.html" in found
    assert "https://site.test/deep/nested/three.html" in found
    assert "https://site.test/deep/nested/four.HTML" in found  # never lowercases the path
    assert "https://site.test/deep/sibling.html" in found  # ../ walks up out of nested/
    # absolute and cross-origin links
    assert "https://site.test/img/favicon.ico" in found
    assert "http://cdn.test/x.png" in found
    assert "https://embed.test/e" in found
    assert "https://svg.test/shape" in found
    assert "https://protocol.relative/x" in found
    # dot segments resolved, ports and IDN normalised
    assert "https://site.test/deep/nested/relative/resolved.html" in found
    assert "https://site.test/default-port" in found
    assert "https://xn--bcher-kva.test/idn" in found
    # things we must not invent: HTML inside comments, <template> and <script> bodies
    assert not any("commented-out" in url for url in found)
    assert "https://site.test/template.html" not in found
    assert "https://site.test/inside-script.html" not in found
    # the noscript fallback is a real link and is kept
    assert "https://site.test/noscript.html" in found
    # srcset from the responsive image
    assert "http://cdn.test/x2.png" in found
    assert "https://site.test/deep/nested/wide.avif" in found
    # lazy-load attributes without src are ignored
    assert "https://site.test/deep/nested/lazy-only.png" not in found
    # non-http schemes were skipped, not extracted
    assert not any(url.startswith(("mailto:", "tel:", "javascript:", "data:")) for url in found)
    assert result.skipped_schemes["mailto"] == 2
    assert result.title == "Acme & Co"
    assert {"anchor-with-id", "legacy-anchor", "section-2"} <= result.anchors


def test_unclosed_tags_still_yield_links() -> None:
    html = (
        '<div><a href="/one.html">one<p>paragraph inside a link <a href="/two.html">two</a></div>'
    )
    result = extract_links(html, "https://site.test/")
    assert set(urls(result)) >= {"https://site.test/one.html", "https://site.test/two.html"}


def test_empty_and_broken_documents() -> None:
    assert urls(extract_links("", BASE)) == []
    assert urls(extract_links("<<<>>>", BASE)) == []
    assert urls(extract_links("<html><body>text only", BASE)) == []


def test_element_labels_are_reported() -> None:
    html = '<img src="/a.png">'
    link = extract_links(html, BASE).links[0]
    assert link.describe() == "<img src>"
    assert link.is_resource
