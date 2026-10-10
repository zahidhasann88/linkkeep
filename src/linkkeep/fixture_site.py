"""A tiny, deliberately broken website used by the integration tests and ``make demo``.

Nothing here is production code: it exists so that ``linkkeep`` can be exercised
end-to-end without the real internet.  Serve it with ``python -m linkkeep.fixture_site``.

Two variants are provided:

``broken``
    the site as it is when you first point Linkkeep at it
``fixed``
    the same site after someone has repaired a few links, so that a second crawl
    demonstrates NEW / FIXED / PERSISTING in ``linkkeep diff``

Deliberate problems: 404, 410, a persistent 503, a slow page, a redirect loop, a
4-hop chain, a 302 that should be a 301, a missing ``#anchor``, a ``robots.txt``
disallow rule, a sitemap with a page that is not linked anywhere, non-HTTP links,
relative URLs, ``<base href>``, ``srcset``, and external links (one reachable, one
not: ``.invalid`` never resolves, so nothing leaves the machine).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import threading
import time
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

#: A 1x1 transparent PNG, used for the fake images on the site.
PNG_PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc00000030101001d8a2b1a0000000049454e44ae426082"
)

VARIANTS = ("broken", "fixed")


def _page(title: str, body: str, *, extra_head: str = "") -> dict[str, Any]:
    html = (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        f'<link rel="stylesheet" href="/assets/app.css">{extra_head}</head>'
        f"<body><h1>{title}</h1>\n{body}\n"
        '<script src="/assets/app.js"></script></body></html>\n'
    )
    return {"status": 200, "content_type": "text/html; charset=utf-8", "body": html}


def routes(port: int, variant: str = "broken") -> dict[str, dict[str, Any]]:
    """The whole fake site as ``path -> response`` (``port`` builds the local externals)."""
    fixed = variant == "fixed"
    base = f"http://127.0.0.1:{port}"
    nav = (
        '<nav><a href="/about">About</a> <a href="/blog">Blog</a> '
        '<a href="/contact">Contact</a></nav>'
    )
    index_links = [
        '<a href="/missing">Missing page</a>',
        '<a href="/gone">Old offer</a>',
        '<a href="/error">Server error page</a>',
        '<a href="/flaky">Careers (flaky)</a>',
        '<a href="/temp-news">News</a>',
        '<a href="/legacy/old">Legacy whitepaper</a>',
        '<a href="/redirect/deep1">Annual report</a>',
        '<a href="/loop-a">Forum</a>',
        '<a href="/private/secret">Internal wiki</a>',
        '<a href="/blog/post-1#section-2">Post, good anchor</a>',
        '<a href="/blog/post-1#nope">Post, bad anchor</a>',
        '<a href="mailto:hello@example.test">Email us</a>',
        '<a href="tel:+15550100">Call us</a>',
        '<a href="javascript:void(0)">Print</a>',
        '<a href="#top">Back to top</a>',
        '<img src="/img/logo.png" alt="Acme logo">',
        '<img src="img/relative.png" alt="Relative image">',
        '<img src="/img/responsive.png" alt="r" srcset="/img/responsive-1x.png 1x, /img/responsive-2x.png 2x">',
        '<iframe src="/embedded"></iframe>',
        '<picture><source srcset="/img/wide.png" src="/img/narrow.png"></picture>',
        f'<a href="{base.replace("127.0.0.1", "localhost")}/ext-ok">Partner status</a>',
        f'<a href="{base.replace("127.0.0.1", "localhost")}/ext-broken">Partner docs</a>',
        '<a href="https://partner.invalid/docs">Unreachable partner</a>',
    ]
    if fixed:
        index_links = [
            link for link in index_links if link != '<a href="/flaky">Careers (flaky)</a>'
        ]
        index_links.append('<a href="/flaky">Careers (now reliable)</a>')

    team_links = ['<a href="/about">Back to about</a>', '<a href="/contact">Contact</a>']
    if not fixed:
        team_links.append('<a href="/gone">Retired product</a>')

    blog_links = ['<a href="/blog/post-1">First post</a>', '<a href="/blog/post-2">Second post</a>']
    if not fixed:
        blog_links.append('<a href="/gone">Archived sale</a>')
    else:
        blog_links.append('<a href="/blog/post-2">Archived sale (fixed)</a>')

    about_links = [
        '<a href="/team">Team</a>',
        '<a href="/careers">Jobs</a>',
        '<a href="/deep/level2">Deep thing</a>',
        '<a href="../about/relative-sibling.html">Relative sibling</a>',
    ]
    if not fixed:
        about_links.append('<a href="/new-and-broken?utm_source=site#top">New landing page</a>')

    table_of_contents = '<p id="section-2">Second section</p>'
    if fixed:
        table_of_contents += '<p id="nope">Now with an anchor</p>'

    site: dict[str, dict[str, Any]] = {
        "/": _page(
            "Acme Widgets",
            nav + "<ul>" + "".join(f"<li>{link}</li>" for link in index_links) + "</ul>",
        ),
        "/about": _page("About Acme", nav + "".join(about_links)),
        "/team": _page("Team", nav + "".join(team_links)),
        "/contact": _page(
            "Contact", nav + '<a href="mailto:hello@example.test">hello@example.test</a>'
        ),
        "/careers": _page(
            "Careers",
            nav + '<a href="level2">Deep via base href</a> <a href="/about">About</a>',
            extra_head='<base href="/deep/">',
        ),
        "/news": _page("News", nav + '<a href="/about">About</a>'),
        "/embedded": _page("Embedded widget", '<a href="/about">Inside iframe</a>'),
        "/blog": _page(
            "Blog", nav + "<ul>" + "".join(f"<li>{link}</li>" for link in blog_links) + "</ul>"
        ),
        "/blog/post-1": _page("First post", table_of_contents + nav + '<a href="/blog">Back</a>'),
        "/blog/post-2": _page("Second post", "<p>Body</p>" + nav),
        "/blog/post-3": _page(
            "Third post (sitemap only)", "<p>Not linked from any page.</p>" + nav
        ),
        "/deep/level2": _page("Level 2", '<a href="/deep/level3">Deeper</a>' + nav),
        "/deep/level3": _page("Level 3", '<a href="/">Home</a>' + nav),
        "/private/secret": _page("Internal wiki", "<p>Disallowed by robots.txt</p>"),
        "/slow": _page("Slow page", "<p>Take your time.</p>") | {"delay": 0.35},
        "/missing": _page("Missing", "gone") | {"status": 404},
        "/new-and-broken": {"status": 404, "content_type": "text/html", "body": "nope"},
        "/gone": {"status": 410, "content_type": "text/html", "body": "gone for good"},
        "/error": {"status": 500, "content_type": "text/html", "body": "kaboom"},
        "/ext-ok": {"status": 200, "content_type": "application/json", "body": '{"ok": true}'},
        "/ext-broken": {"status": 404, "content_type": "text/html", "body": "gone"},
        "/flaky": {
            "status": 503,
            "content_type": "text/html",
            "body": "busy, try later",
            "flaky": True,
        },
        "/about/relative-sibling.html": {
            "status": 404,
            "content_type": "text/html",
            "body": "this path does not exist",
        },
        "/robots.txt": {
            "status": 200,
            "content_type": "text/plain; charset=utf-8",
            "body": (
                "User-agent: *\nDisallow: /private/\n# Crawl-delay omitted on purpose (fast tests)\n"
                f"Sitemap: {base}/sitemap.xml\n"
            ),
        },
        "/sitemap.xml": {
            "status": 200,
            "content_type": "application/xml",
            "body": (
                '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + "".join(
                    f"  <url><loc>{base}{path}</loc></url>\n"
                    for path in ("/blog/post-3", "/team", "/contact", "/missing", "/slow")
                )
                + "</urlset>\n"
            ),
        },
        "/assets/app.css": {"status": 200, "content_type": "text/css", "body": "body{color:#111}"},
        "/assets/app.js": {
            "status": 200,
            "content_type": "text/javascript",
            "body": "console.log(1)",
        },
        "/manifest.json": {"status": 200, "content_type": "application/json", "body": "{}"},
        "/redirect/1": {"status": 301, "location": "/redirect/2"},
        "/redirect/2": {"status": 301, "location": "/redirect/3"},
        "/redirect/3": {"status": 301, "location": "/redirect/deep1"},
        "/redirect/deep1": {"status": 301, "location": "/redirect/2"},
        "/legacy/old": {"status": 301, "location": "/legacy/new"},
        "/legacy/new": {"status": 301, "location": "/about"},
        "/temp-news": {"status": 301 if fixed else 302, "location": "/news"},
        "/loop-a": {"status": 301, "location": "/loop-b"},
        "/loop-b": {"status": 301, "location": "/loop-a"},
        "/big-page.html": {
            "status": 200,
            "content_type": "text/html",
            "body": "<html><body>"
            + ("x" * 4000)
            + '<a href="/about">Still linked</a></body></html>',
        },
    }
    if fixed:
        site["/missing"] = _page("Missing page (restored)", "<p>We fixed it.</p>" + nav)
        site["/error"] = _page("Server error page (restored)", "<p>All good now.</p>" + nav)
        site["/flaky"] = {"status": 200, "content_type": "text/html", "body": "<p>Jobs</p>"}
        for image in (
            "/img/logo.png",
            "/img/relative.png",
            "/img/responsive.png",
            "/img/responsive-1x.png",
            "/img/responsive-2x.png",
            "/img/wide.png",
            "/img/narrow.png",
        ):
            site[image] = {"status": 200, "content_type": "image/png", "body": PNG_PIXEL}
    else:
        site["/img/logo.png"] = {"status": 200, "content_type": "image/png", "body": PNG_PIXEL}
    return site


class LinkkeepFixtureHandler(BaseHTTPRequestHandler):
    """Serves :func:`routes`; also counts hits so tests can assert on politeness."""

    server_version = "LinkkeepFixture/0.1"
    protocol_version = "HTTP/1.1"
    routes_by_path: ClassVar[dict[str, dict[str, Any]]] = {}
    hits: ClassVar[Counter[str]] = Counter()
    user_agents: ClassVar[set[str]] = set()

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""
        del format, args

    # -- verbs -----------------------------------------------------------------
    def do_GET(self) -> None:
        self._respond(head=False)

    def do_HEAD(self) -> None:
        self._respond(head=True)

    # -- implementation --------------------------------------------------------
    def _route(self, path: str) -> dict[str, Any] | None:
        route = self.routes_by_path.get(path)
        if route is None:
            return None
        if route.get("flaky") and self.server.is_fixed:  # type: ignore[attr-defined]
            return {"status": 200, "content_type": "text/html", "body": "<p>Jobs</p>"}
        if route.get("flaky"):
            count = self.server.note_flaky()  # type: ignore[attr-defined]
            if count > 2:
                return {"status": 200, "content_type": "text/html", "body": "<p>Jobs</p>"}
        return route

    def _respond(self, *, head: bool) -> None:
        parts = urlsplit(self.path)
        path = parts.path or "/"
        self.hits[path] += 1
        self.user_agents.add(self.headers.get("User-Agent", ""))
        route = self._route(path)
        if route is None:
            body = f"<html><body><h1>404</h1><p>no route for {path}</p></body></html>".encode()
            self._write(
                404,
                {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))},
                body,
                head,
            )
            return
        delay = route.get("delay")
        if delay:
            time.sleep(float(delay))
        headers: dict[str, str] = {}
        location = route.get("location")
        if location:
            target = (
                location
                if "://" in location
                else f"http://{self.headers.get('Host', '127.0.0.1')}{location}"
            )
            headers["Location"] = target
        body_bytes = b""
        if "body" in route:
            raw = route["body"]
            body_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        headers["Content-Type"] = str(route.get("content_type", "text/html; charset=utf-8"))
        headers["Content-Length"] = str(0 if head else len(body_bytes))
        headers["Date"] = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self._write(int(route.get("status", 200)), headers, b"" if head else body_bytes, head)

    def _write(self, status: int, headers: dict[str, str], body: bytes, head: bool) -> None:
        del head
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)


class FixtureServer(ThreadingHTTPServer):
    """Threaded HTTP server whose content can be swapped between requests."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, variant: str = "broken") -> None:
        self.variant = variant
        self.flaky_hits = 0
        self._flaky_lock = threading.Lock()
        self.lock = threading.Lock()
        self._bound_handler: type[LinkkeepFixtureHandler] = type(
            "BoundHandler",
            (LinkkeepFixtureHandler,),
            {"routes_by_path": {}, "hits": Counter(), "user_agents": set()},
        )
        super().__init__(address, self._bound_handler)
        self.port = int(self.server_address[1])
        self.rebind_routes()

    # -- state ----------------------------------------------------------------
    def rebind_routes(self) -> None:
        self._bound_handler.routes_by_path = routes(self.port, self.variant)

    def set_variant(self, variant: str) -> None:
        """Change what the site serves *without* restarting it (the "someone fixed it" step)."""
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}")
        with self.lock:
            self.variant = variant
            self.flaky_hits = 0
            self.rebind_routes()

    @property
    def is_fixed(self) -> bool:
        return self.variant == "fixed"

    def note_flaky(self) -> int:
        with self._flaky_lock:
            self.flaky_hits += 1
            return self.flaky_hits

    # -- introspection --------------------------------------------------------
    @property
    def handler(self) -> type[LinkkeepFixtureHandler]:
        return self._bound_handler

    def hits(self, path: str | None = None) -> int | Counter[str]:
        counts = self.handler.hits
        return counts if path is None else counts[path]

    def reset_counters(self) -> None:
        self.handler.hits.clear()
        self.handler.user_agents.clear()
        self.flaky_hits = 0

    @property
    def seen_user_agents(self) -> set[str]:
        return set(self.handler.user_agents)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@contextlib.contextmanager
def running_site(*, variant: str = "broken", port: int = 0) -> Iterator[FixtureServer]:
    """Context manager that serves the fixture site on a background thread."""
    server = FixtureServer(("127.0.0.1", port), variant=variant)
    thread = threading.Thread(
        target=server.serve_forever, name="linkkeep-fixture-site", daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - manual helper
    """Serve the fixture site until Ctrl+C (``python -m linkkeep.fixture_site``).

    The serving loop lives in :func:`running_site`, which is what the test suite uses -
    one code path, so the manual helper cannot rot into a server that never accepts.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m linkkeep.fixture_site",
        description="Serve the Linkkeep fixture site on 127.0.0.1 until Ctrl+C.",
    )
    parser.add_argument("--port", type=int, default=8077, help="port to bind (0 = ephemeral)")
    parser.add_argument("--variant", choices=VARIANTS, default="broken")
    args = parser.parse_args(argv)

    with running_site(variant=args.variant, port=args.port) as server:
        print(f"serving {args.variant} fixture site at {server.url} (Ctrl+C to stop)", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("stopped", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
