"""HTML link extraction.

Only the attributes listed in :data:`ELEMENT_ATTRIBUTES` are mined, plus ``srcset``
on ``img``/``source``.  We use selectolax' lexbor backend: it is fast, tolerant of
messy markup and gives us ``<base href>`` handling for free.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from linkkeep.urlnorm import SlashPolicy, is_non_http_scheme, normalise_url, parse_url, resolve_url

#: ``tag -> attribute`` pairs we extract from.  Order matters only for tie-breaking.
ELEMENT_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("a", "href"),
    ("link", "href"),
    ("img", "src"),
    ("script", "src"),
    ("iframe", "src"),
    ("source", "src"),
)

#: Elements that make up the page itself (mixed-content checks apply to these, but not
#: to plain ``<a href>`` navigation links, which would be pure noise).
RESOURCE_ELEMENTS: frozenset[str] = frozenset({"img", "script", "source", "iframe", "link"})

#: Elements we look at: each ``(tag, attribute)`` pair above, plus ``img``/``source``
#: that only carry a ``srcset`` (common in ``<picture>`` art direction).
SELECTOR = ", ".join(
    [f"{tag}[{attr}]" for tag, attr in ELEMENT_ATTRIBUTES] + ["img[srcset]", "source[srcset]"]
)

MAX_LINK_TEXT = 200
_WHITESPACE_RE = re.compile(r"\s+")
_SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")
_FRAGMENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:+-]*$")


@dataclass(frozen=True)
class ExtractedLink:
    """One link as found in a document, already resolved to an absolute URL."""

    url: str
    element: str
    attribute: str
    raw: str
    fragment: str = ""
    link_text: str | None = None
    order: int = 0

    @property
    def is_resource(self) -> bool:
        return self.element in RESOURCE_ELEMENTS

    def describe(self) -> str:
        """``<img src="...">`` style label used in reports and the database."""
        return f"<{self.element} {self.attribute}>"


@dataclass
class ExtractResult:
    links: list[ExtractedLink] = field(default_factory=list)
    #: Why things were not turned into links: ``scheme``, ``fragment-only``,
    #: ``unresolvable``, ``anchor-name``.
    skipped: dict[str, int] = field(default_factory=dict)
    #: Per-scheme counts for non-HTTP links (``mailto``, ``tel``, ``javascript``, ...).
    skipped_schemes: dict[str, int] = field(default_factory=dict)
    base_href: str | None = None
    anchors: set[str] = field(default_factory=set)
    title: str | None = None
    is_html: bool = True

    @property
    def http_links(self) -> Iterator[ExtractedLink]:
        yield from self.links

    def count_skip(self, key: str, *, scheme: str | None = None) -> None:
        self.skipped[key] = self.skipped.get(key, 0) + 1
        if scheme:
            self.skipped_schemes[scheme] = self.skipped_schemes.get(scheme, 0) + 1


def parse_srcset(value: str) -> list[str]:
    """Return the URLs of a ``srcset`` attribute (``"a.png 1x, b.png 2x"``)."""
    urls: list[str] = []
    for candidate in _SRCSET_SPLIT_RE.split(value.strip()):
        if not candidate:
            continue
        url = candidate.split()[0]
        if url:
            urls.append(url)
    return urls


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _WHITESPACE_RE.sub(" ", value).strip()
    if not text:
        return None
    if len(text) > MAX_LINK_TEXT:
        text = text[: MAX_LINK_TEXT - 1].rstrip() + "\u2026"
    return text


def _base_href(parser: LexborHTMLParser) -> str | None:
    node = parser.css_first("base[href]")
    if node is None:
        return None
    value = node.attributes.get("href")
    return value.strip() if value and value.strip() else None


def extract_links(
    html: str,
    page_url: str,
    *,
    sort_query: bool = False,
    slash_policy: SlashPolicy = "keep",
    include_fragments: bool = False,
) -> ExtractResult:
    """Extract and resolve every link of interest from ``html``.

    ``page_url`` is the URL the document was served from (``<base href>`` wins when the
    document declares one).  Relative references, IDN hosts, default ports and
    fragments are handled here so callers only ever see normalised absolute URLs.
    """
    result = ExtractResult()
    try:
        parser = LexborHTMLParser(html)
    except Exception:  # pragma: no cover - lexbor is extremely tolerant
        result.is_html = False
        return result

    page = parse_url(page_url)
    if page is None:
        return result

    base = _base_href(parser)
    if base:
        result.base_href = base
        resolved_base = resolve_url(page.url, base) or page.url
        base_parse = parse_url(resolved_base)
        if base_parse is not None:
            page = base_parse
    effective_base = page.url

    title_node = parser.css_first("title")
    if title_node is not None:
        result.title = _clean_text(title_node.text())

    order = 0
    seen: set[tuple[str, str, str]] = set()
    for node in parser.css(SELECTOR):
        tag = node.tag or ""
        wanted = dict(ELEMENT_ATTRIBUTES).get(tag)
        attrs: dict[str, Any] = dict(node.attributes)
        candidates: list[tuple[str, str]] = []
        if wanted:
            value = attrs.get(wanted)
            if value is not None:
                candidates.append((wanted, value))
            elif tag == "a" and "name" in attrs:
                result.count_skip("anchor-name")
        if tag in {"img", "source"} and attrs.get("srcset"):
            for url in parse_srcset(str(attrs["srcset"])):
                candidates.append(("srcset", url))
        if not candidates:
            continue
        text = _clean_text(attrs.get("alt")) or ""
        if tag == "a":
            text = _clean_text(node.text(separator=" ", strip=True)) or text
        for attribute, raw in candidates:
            raw_str = str(raw)
            if is_non_http_scheme(raw_str):
                scheme = raw_str.split(":", 1)[0].lower() if ":" in raw_str else "other"
                result.count_skip("scheme", scheme=scheme)
                continue
            absolute: str | None
            if raw_str.startswith("#"):
                result.count_skip("fragment-only")
                if not include_fragments or raw_str == "#":
                    continue
                absolute = effective_base.split("#", 1)[0] + raw_str
            else:
                absolute = resolve_url(effective_base, raw_str)
            if absolute is None:
                result.count_skip("unresolvable")
                continue
            fragment = ""
            hash_at = absolute.find("#")
            if hash_at != -1:
                fragment = absolute[hash_at + 1 :]
            normalised = normalise_url(absolute, sort_query=sort_query, slash_policy=slash_policy)
            if normalised is None:
                result.count_skip("unresolvable")
                continue
            key = (normalised, fragment, attribute)
            if key in seen:
                continue
            seen.add(key)
            result.links.append(
                ExtractedLink(
                    url=normalised,
                    fragment=fragment,
                    element=tag,
                    attribute=attribute,
                    raw=raw_str,
                    link_text=text or None,
                    order=order,
                )
            )
            order += 1

    result.anchors = collect_anchors(html)
    return result


def collect_anchors(html: str) -> set[str]:
    """All ``id`` values plus legacy ``<a name="...">`` targets of a document."""
    parser = LexborHTMLParser(html)
    anchors: set[str] = set()
    for node in parser.css("[id]"):
        value = (node.attributes.get("id") or "").strip()
        if value:
            anchors.add(value)
    for node in parser.css("a[name]"):
        value = (node.attributes.get("name") or "").strip()
        if value:
            anchors.add(value)
    return anchors


def fragment_is_valid(fragment: str) -> bool:
    """A fragment we are willing to check: bare names only, no selectors or JSON."""
    return bool(_FRAGMENT_RE.match(fragment))
