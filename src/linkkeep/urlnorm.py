"""URL resolution, normalisation and host comparison.

Design notes
------------
* We deliberately do *not* depend on a public-suffix database.  ``registrable_host``
  uses a small built-in list of two-level public suffixes plus a "last two labels"
  rule, which is the correct behaviour for the overwhelmingly common case and never
  requires the network.  Pass ``--include-subdomains`` (or a different root) when the
  heuristic is not what you want.
* Normalisation is intentionally conservative: it lowercases the scheme/host, removes
  the default port, drops the fragment and percent-decodes nothing.  ``sort_query`` and
  ``slash_policy`` are opt-in because both can merge URLs a site treats as different.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

SlashPolicy = Literal["keep", "strip", "add"]

#: Schemes we never try to fetch (they are counted in the summary instead).
NON_HTTP_SCHEMES: frozenset[str] = frozenset(
    {
        "mailto",
        "tel",
        "sms",
        "callto",
        "javascript",
        "data",
        "blob",
        "file",
        "ftp",
        "ftps",
        "sftp",
        "ssh",
        "ws",
        "wss",
        "chrome",
        "intent",
        "whatsapp",
        "slack",
        "magnet",
        "cid",
        "about",
        "unknown",
    }
)

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

#: Hosts that are always treated as private/loopback even though they are not IPs.
PRIVATE_HOST_NAMES: frozenset[str] = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "wpad"}
)

#: Suffixes whose "registrable host" keeps three labels (a plain two-label cut would
#: be wrong, e.g. www.example.co.uk -> "co.uk" would merge unrelated sites).
TWO_LEVEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "net.uk",
        "co.jp",
        "or.jp",
        "ne.jp",
        "ac.jp",
        "go.jp",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "co.nz",
        "net.nz",
        "org.nz",
        "govt.nz",
        "com.br",
        "net.br",
        "org.br",
        "gov.br",
        "co.in",
        "net.in",
        "org.in",
        "gov.in",
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "co.za",
        "org.za",
        "web.za",
        "com.tr",
        "org.tr",
        "gov.tr",
        "com.mx",
        "org.mx",
        "gob.mx",
        "co.kr",
        "or.kr",
        "go.kr",
        "com.sg",
        "edu.sg",
        "gov.sg",
        "com.hk",
        "org.hk",
        "gov.hk",
        "com.my",
        "edu.my",
        "gov.my",
        "co.id",
        "or.id",
        "go.id",
        "com.pk",
        "edu.pk",
        "gok.pk",
        "com.bd",
        "edu.bd",
        "ac.bd",
        "gov.bd",
    }
)

_IDN_RE = re.compile(r"[^\x00-\x7f]")


@dataclass(frozen=True)
class ParsedUrl:
    """Cheap decomposition of an absolute URL."""

    url: str
    scheme: str
    host: str
    port: int | None
    path: str
    query: str
    fragment: str

    @property
    def hostname(self) -> str:
        """Lowercased, punycoded host without port."""
        return self.host

    @property
    def origin(self) -> str:
        if self.port and self.port != DEFAULT_PORTS.get(self.scheme):
            return f"{self.scheme}://{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}"

    @property
    def without_fragment(self) -> str:
        return urlunsplit(
            (self.scheme, _netloc(self.host, self.port, self.scheme), self.path, self.query, "")
        )


def _netloc(host: str, port: int | None, scheme: str) -> str:
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return host
    return f"{host}:{port}"


def _encode_host(host: str) -> str:
    """Lowercase and IDNA-encode a host name; IPv6 literals are left alone."""
    host = host.strip().rstrip(".")
    if host.startswith("[") or ":" in host:
        return host.lower()
    if _IDN_RE.search(host):
        try:
            return host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return host.lower()
    return host.lower()


def parse_url(url: str) -> ParsedUrl | None:
    """Parse an absolute http(s)-ish URL; returns ``None`` if there is no host."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if not scheme:
        return None
    try:
        port = parts.port
    except ValueError:
        port = None
    try:
        host = _encode_host(parts.hostname or "")
    except ValueError:
        return None
    if not host:
        return None
    return ParsedUrl(
        url=url,
        scheme=scheme,
        host=host,
        port=port,
        path=parts.path or "/",
        query=parts.query,
        fragment=parts.fragment,
    )


def scheme_of(url: str) -> str:
    """Return the lowercase scheme of ``url`` (``""`` for relative references)."""
    candidate = url.strip()
    if candidate.startswith("//"):
        return ""
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", candidate)
    return match.group(1).lower() if match else ""


def is_non_http_scheme(url: str) -> bool:
    """True for ``mailto:``, ``tel:``, ``javascript:`` and friends."""
    scheme = scheme_of(url)
    if not scheme or scheme in {"http", "https"}:
        return False
    return scheme in NON_HTTP_SCHEMES or not scheme.startswith("http")


def resolve_url(base: str, href: str) -> str | None:
    """Resolve ``href`` against ``base``; ``None`` when the result is unusable."""
    raw = (href or "").strip()
    # Angle-bracket wrapped targets are legal in srcset and occasionally hand-written.
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    if not raw or raw.startswith("#"):
        return None
    if is_non_http_scheme(raw):
        return None
    candidate = raw
    if candidate.startswith("//"):
        parsed = parse_url(base)
        candidate = f"{parsed.scheme if parsed else 'https'}:{candidate}"
    try:
        joined = urljoin(base, candidate)
    except ValueError:
        return None
    split = urlsplit(joined)
    if split.scheme not in {"http", "https"} or not split.netloc:
        return None
    return joined


def normalise_url(
    url: str,
    *,
    strip_fragment: bool = True,
    sort_query: bool = False,
    slash_policy: SlashPolicy = "keep",
    index_names: Iterable[str] = ("index.html", "index.htm", "default.html"),
) -> str | None:
    """Canonical form used for de-duplication and for issue fingerprints.

    The fragment is always dropped from the *fetch* target; callers that need it keep
    it themselves (see :func:`fragment_of`).  ``strip_fragment=False`` is accepted for
    readability at call sites that explicitly do not care.
    """
    parsed = parse_url(url)
    if parsed is None:
        return None
    path = parsed.path or "/"
    for name in tuple(index_names):
        if path.endswith(f"/{name}"):
            path = path[: -len(name)]
            break
    if slash_policy == "strip" and path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    elif slash_policy == "add" and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path = f"{path}/"
    query = parsed.query
    if sort_query and query:
        query = urlencode(sorted(parse_qsl(query, keep_blank_values=True)))
    if not strip_fragment:
        return urlunsplit(
            (
                parsed.scheme,
                _netloc(parsed.host, parsed.port, parsed.scheme),
                path,
                query,
                parsed.fragment,
            )
        )
    return urlunsplit(
        (parsed.scheme, _netloc(parsed.host, parsed.port, parsed.scheme), path, query, "")
    )


def canonical_url(url: str) -> str:
    """``normalise_url`` with the default policy that never returns ``None``."""
    return normalise_url(url) or url


def registrable_host(host: str) -> str:
    """The "site" part of a host: ``a.b.example.co.uk`` -> ``example.co.uk``."""
    host = _encode_host(host)
    if not host:
        return ""
    if is_ip_literal(host):
        return host
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    last_two = ".".join(labels[-2:])
    if last_two in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def is_ip_literal(host: str) -> bool:
    host = host.strip().strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def same_site(a: str, b: str, *, include_subdomains: bool = False) -> bool:
    """Are two URLs the "same site"?

    ``include_subdomains=False`` (the default) requires the same hostname, tolerating a
    leading ``www.`` in either direction, which keeps a typo'd flag from accidentally
    crawling every subdomain a host has.  ``include_subdomains=True`` compares
    registrable hosts instead, so ``blog.example.com`` counts as internal to
    ``example.com``.
    """
    pa, pb = parse_url(a), parse_url(b)
    if pa is None or pb is None:
        return False
    if include_subdomains:
        registrable = registrable_host(pa.host)
        return bool(registrable) and registrable == registrable_host(pb.host)
    return _bare_host(pa.host) == _bare_host(pb.host)


def _bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def is_private_host(host: str) -> bool:
    """Basic SSRF guard: loopback, RFC1918, link-local, reserved and *.local style names."""
    host = _encode_host(host).strip("[]")
    if not host:
        return True
    if host in PRIVATE_HOST_NAMES or host.endswith(
        (".local", ".internal", ".localhost", ".home.arpa")
    ):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def strip_fragment(url: str) -> str:
    split = urlsplit(url)
    if not split.fragment:
        return url
    return urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))


def fragment_of(url: str) -> str:
    return urlsplit(url).fragment


NON_HTML_SUFFIXES: tuple[str, ...] = (
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".xml",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".map",
    ".zip",
    ".gz",
    ".tgz",
    ".tar",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp4",
    ".webm",
    ".mp3",
    ".ogg",
    ".wav",
    ".wasm",
    ".dmg",
    ".exe",
)


def is_probably_html_path(url: str) -> bool:
    """Heuristic used only to decide what to queue when a page has no content type."""
    path = urlsplit(url).path.lower()
    return not path.endswith(NON_HTML_SUFFIXES)


def describe_target(url: str, limit: int = 90) -> str:
    """Compact URL for terminal output (keeps host + path, drops long queries)."""
    parsed = parse_url(url)
    if parsed is None:
        return url[:limit]
    tail = parsed.path if parsed.path != "/" else "/"
    if parsed.query:
        tail = f"{tail}?{parsed.query}"
    text = f"{parsed.host}{tail}"
    return text if len(text) <= limit else f"{text[: limit - 1]}\u2026"
