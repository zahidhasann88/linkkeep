"""URL resolution and normalisation edge cases."""

from __future__ import annotations

import pytest

from linkkeep.urlnorm import (
    canonical_url,
    describe_target,
    fragment_of,
    is_ip_literal,
    is_non_http_scheme,
    is_private_host,
    is_probably_html_path,
    normalise_url,
    parse_url,
    registrable_host,
    resolve_url,
    same_site,
    scheme_of,
    strip_fragment,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # relative resolution against a page
        ("about.html", "https://site.test/blog/about.html"),
        ("./about.html", "https://site.test/blog/about.html"),
        ("../about.html", "https://site.test/about.html"),
        ("/about", "https://site.test/about"),
        ("about", "https://site.test/blog/about"),
        ("#section", None),
        ("?x=1", "https://site.test/blog/post.html?x=1"),
        ("//cdn.test/a.js", "https://cdn.test/a.js"),
        ("https://other.test/x", "https://other.test/x"),
        ("", None),
        ("   ", None),
        ("mailto:a@b.test", None),
        ("javascript:void(0)", None),
        ("data:image/png;base64,aaa", None),
        ("tel:+15550100", None),
        ("<[img.png]>", "https://site.test/blog/[img.png]"),
    ],
)
def test_resolve_relative(raw: str, expected: str | None) -> None:
    base = "https://site.test/blog/post.html"
    assert resolve_url(base, raw) == expected


def test_resolve_angle_brackets_and_whitespace() -> None:
    base = "https://site.test/blog/"
    assert resolve_url(base, "  <../img/a.png>  ") == "https://site.test/img/a.png"
    assert resolve_url(base, "<a.png>") == "https://site.test/blog/a.png"


def test_base_href_is_respected_when_caller_uses_it() -> None:
    # the caller resolves <base href> first, then uses that as the base
    base = resolve_url("https://site.test/deep/page.html", "https://site.test/assets/")
    assert base is not None
    assert resolve_url(base, "logo.png") == "https://site.test/assets/logo.png"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Site.TEST:443/a", "https://site.test/a"),
        ("https://site.test:8443/a", "https://site.test:8443/a"),
        ("http://site.test:80/a", "http://site.test/a"),
        ("https://site.test/a/#frag", "https://site.test/a/"),
        ("https://site.test/a?b=2&c=3", "https://site.test/a?b=2&c=3"),
        ("https://site.test//a", "https://site.test//a"),
        ("https://site.test", "https://site.test/"),
        ("https://site.test/", "https://site.test/"),
        ("https://xn--ho-hia.de/x", "https://xn--ho-hia.de/x"),
    ],
)
def test_normalise_defaults(raw: str, expected: str) -> None:
    assert normalise_url(raw) == expected


def test_normalise_query_sorting() -> None:
    url = "https://site.test/s?q=2&r=1&q=3"
    assert normalise_url(url) == url
    assert normalise_url(url, sort_query=True) == "https://site.test/s?q=2&q=3&r=1"


def test_normalise_query_sorting_keeps_blanks_and_orders() -> None:
    url = "https://site.test/s?b=&a=1"
    assert normalise_url(url, sort_query=True) == "https://site.test/s?a=1&b="


@pytest.mark.parametrize(
    ("policy", "root", "path", "file"),
    [
        ("keep", "https://site.test/", "https://site.test/a/", "https://site.test/a.html"),
        ("strip", "https://site.test/", "https://site.test/a", "https://site.test/a.html"),
        ("add", "https://site.test/", "https://site.test/a/", "https://site.test/a.html"),
    ],
)
def test_trailing_slash_policy(policy: str, root: str, path: str, file: str) -> None:
    assert normalise_url("https://site.test/", slash_policy=policy) == root  # type: ignore[arg-type]
    assert normalise_url("https://site.test/a/", slash_policy=policy) == path  # type: ignore[arg-type]
    assert normalise_url("https://site.test/a.html", slash_policy=policy) == file  # type: ignore[arg-type]


def test_index_documents_are_folded_away() -> None:
    assert normalise_url("https://site.test/index.html") == "https://site.test/"
    assert normalise_url("https://site.test/blog/index.htm") == "https://site.test/blog/"


def test_idn_and_uppercase_hosts() -> None:
    assert normalise_url("https://Bücher.example/ä") == "https://xn--bcher-kva.example/ä"
    parsed = parse_url("https://Bücher.example/x")
    assert parsed is not None
    assert parsed.host == "xn--bcher-kva.example"


def test_fragment_helpers() -> None:
    assert fragment_of("https://site.test/a#b") == "b"
    assert strip_fragment("https://site.test/a?x=1#b") == "https://site.test/a?x=1"
    assert strip_fragment("https://site.test/a") == "https://site.test/a"


def test_normalise_returns_none_for_garbage() -> None:
    assert normalise_url("not a url") is None
    assert canonical_url("not a url") == "not a url"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.example.com", "example.com"),
        ("example.com", "example.com"),
        ("a.b.example.com", "example.com"),
        ("www.example.co.uk", "example.co.uk"),
        ("blog.example.co.uk", "example.co.uk"),
        ("a.b.example.jp", "example.jp"),
        ("sub.example.xyz", "example.xyz"),
        ("127.0.0.1", "127.0.0.1"),
        ("", ""),
    ],
)
def test_registrable_host(host: str, expected: str) -> None:
    assert registrable_host(host) == expected


def test_same_site_default_is_exact_host_with_www_tolerance() -> None:
    assert same_site("https://www.example.com/a", "https://example.com/b")
    assert not same_site("https://blog.example.com/a", "https://example.com/b")
    assert same_site("https://blog.example.com/a", "https://example.com/b", include_subdomains=True)
    assert not same_site("https://evil.test/x", "https://example.com/b", include_subdomains=True)
    assert not same_site("relative", "https://example.com/b")


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.42.1.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "0.0.0.0",
        "localhost",
        "db.internal",
        "printer.local",
    ],
)
def test_private_hosts(host: str) -> None:
    assert is_private_host(host)


@pytest.mark.parametrize("host", ["example.com", "8.8.8.8", "1.1.1.1", "www.google.com"])
def test_public_hosts(host: str) -> None:
    assert not is_private_host(host)


def test_ip_literal_detection() -> None:
    assert is_ip_literal("127.0.0.1")
    assert is_ip_literal("[::1]")
    assert not is_ip_literal("localhost")


@pytest.mark.parametrize(
    "url", ["mailto:a@b.c", "tel:123", "javascript:x", "data:text/plain,x", "file:///etc"]
)
def test_non_http_schemes(url: str) -> None:
    assert is_non_http_scheme(url)


@pytest.mark.parametrize(
    "url", ["https://a.test", "http://a.test", "/relative", "a.html", "//cdn.test/x"]
)
def test_http_schemes_are_not_skipped(url: str) -> None:
    assert not is_non_http_scheme(url)


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("HTTPS://a.test", "https"),
        ("ftp://a.test", "ftp"),
        ("/x", ""),
        ("#frag", ""),
        ("//cdn.test/x", ""),
    ],
)
def test_scheme_of(url: str, scheme: str) -> None:
    assert scheme_of(url) == scheme


def test_is_probably_html_path() -> None:
    assert is_probably_html_path("https://a.test/page.html")
    assert is_probably_html_path("https://a.test/page")
    assert not is_probably_html_path("https://a.test/x.zip")
    assert not is_probably_html_path("https://a.test/x.pdf")


def test_describe_target_shortens_and_keeps_host() -> None:
    assert describe_target("https://site.test/blog/post") == "site.test/blog/post"
    long = describe_target("https://site.test/" + "x" * 200, limit=30)
    assert len(long) <= 30
    assert describe_target("garbage value here") == "garbage value here"


def test_ports_are_kept_when_non_default() -> None:
    parsed = parse_url("https://site.test:8443/a")
    assert parsed is not None
    assert parsed.port == 8443
    assert parsed.origin == "https://site.test:8443"
    assert parsed.without_fragment == "https://site.test:8443/a"


def test_invalid_port_is_tolerated() -> None:
    parsed = parse_url("https://site.test:abc/a")
    assert parsed is not None
    assert parsed.port is None
