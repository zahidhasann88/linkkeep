"""Configuration: defaults, ``linkkeep.yaml`` loading, CLI overrides, URL filters."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping
from fnmatch import translate as glob_to_regex
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from linkkeep import __version__

#: Edit this to your own GitHub location; it also appears in the User-Agent.
REPO_URL = "https://github.com/YOUR-OWNER/linkkeep"

CONFIG_FILENAMES: tuple[str, ...] = ("linkkeep.yaml", "linkkeep.yml", ".linkkeep.yaml")
FailOn = Literal["error", "warning", "info", "never"]
ReportFormat = Literal["text", "json", "markdown", "html", "junit", "none"]

DEFAULT_USER_AGENT = f"Linkkeep/{__version__} (+{REPO_URL})"


class LinkkeepError(Exception):
    """Base class for expected, user-friendly failures (exit code 2)."""


class ConfigError(LinkkeepError):
    """Bad config file, bad flag value, or unwritable database (exit code 2)."""


class CrawlRunError(LinkkeepError):
    """The crawl could not run at all, e.g. every seed is unreachable (exit code 3)."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class CrawlSection(_Section):
    """Engine limits."""

    max_pages: int = Field(
        default=1000, ge=1, description="Maximum internal pages to fetch per run"
    )
    max_depth: int | None = Field(
        default=None, ge=0, description="Maximum link depth from a seed (0 = seeds only)"
    )
    concurrency: int = Field(default=10, ge=1, le=200, description="Simultaneous requests")
    rate: float = Field(default=5.0, gt=0, description="Requests per second, per host")
    timeout: float = Field(default=15.0, gt=0, description="Per-request timeout in seconds")
    retries: int = Field(
        default=1, ge=0, le=5, description="Retries for transient errors (timeout, 5xx, 429)"
    )
    max_redirect_hops: int = Field(
        default=3, ge=0, description="Redirects followed before redirect_chain"
    )
    max_response_size: int = Field(
        default=2_000_000,
        ge=1024,
        description="Do not parse HTML bodies larger than this many bytes",
    )

    @field_validator("max_depth", mode="before")
    @classmethod
    def _unlimited_depth(cls, value: Any) -> Any:
        if value in (None, "", "unlimited", -1):
            return None
        return value


class RulesSection(_Section):
    """Which URLs are internal, which are checked, and what is skipped."""

    include_subdomains: bool = Field(default=False, description="Treat subdomains as internal")
    include: list[str] = Field(
        default_factory=list, description="Only crawl/check URLs matching these patterns"
    )
    exclude: list[str] = Field(
        default_factory=list, description="Skip URLs matching these patterns"
    )
    ignore_status: list[int] = Field(
        default_factory=list,
        description="Statuses to ignore, e.g. [999] for LinkedIn's anti-bot response",
    )
    check_fragments: bool = Field(
        default=False, description="Verify #anchors exist on the target page"
    )
    sitemap: bool = Field(
        default=False, description="Also queue URLs from /sitemap.xml (and robots.txt Sitemap:)"
    )
    follow_external: bool = Field(default=True, description="Status-check external links")
    allow_private: bool = Field(default=False, description="Allow crawling loopback/RFC1918 hosts")
    ignore_robots: bool = Field(default=False, description="Ignore robots.txt (not recommended)")
    url_trailing_slash: Literal["keep", "strip", "add"] = Field(
        default="keep", description="Trailing slash policy used for de-duplication"
    )
    url_sort_query: bool = Field(
        default=False, description="Sort query parameters before de-duplicating"
    )


class DetectionSection(_Section):
    """Thresholds for the issue detectors."""

    slow_ms: int = Field(
        default=5000, ge=1, description="Responses slower than this produce a `slow` info issue"
    )
    mixed_content: bool = Field(default=True, description="Flag http resources on https pages")
    temporary_redirect_hint: bool = Field(
        default=True, description="Flag 302/307 on internal links as info"
    )


class AuthSection(_Section):
    """Headers/cookies for protected sites. Values may be ``env:VAR_NAME``.

    Values are never printed, logged or stored - only their names are.
    """

    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)


class OutputSection(_Section):
    fail_on: FailOn = Field(default="error", description="Severity that makes the command exit 1")
    format: ReportFormat = Field(default="text", description="Default report format")
    color: bool = True
    quiet: bool = False
    verbose: bool = False


class WatchSection(_Section):
    every: str = Field(default="6h", description="Interval, e.g. 30m, 6h, 1d")
    notify_webhook: str | None = Field(
        default=None, description="POST a JSON summary when NEW issues appear"
    )


class Config(_Section):
    """Everything Linkkeep knows about a run."""

    site: str | None = Field(default=None, description="Default seed URL")
    user_agent: str = Field(default=DEFAULT_USER_AGENT)
    database: str | None = Field(default=None, description="Path to the SQLite file")
    respect_crawl_delay: bool = Field(
        default=True, description="Honour Crawl-delay from robots.txt"
    )
    crawl: CrawlSection = Field(default_factory=CrawlSection)
    rules: RulesSection = Field(default_factory=RulesSection)
    detection: DetectionSection = Field(default_factory=DetectionSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    output: OutputSection = Field(default_factory=OutputSection)
    watch: WatchSection = Field(default_factory=WatchSection)

    # -- loading ---------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None, *, overrides: Mapping[str, Any] | None = None) -> Config:
        data: dict[str, Any] = {}
        if path is not None:
            data = read_yaml_config(path)
        merged = deep_merge(data, prune_nones(overrides or {}))
        try:
            return cls.model_validate(merged)
        except ValidationError as exc:
            raise ConfigError(
                format_validation_error(exc, str(path) if path else "options")
            ) from exc

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Config:
        try:
            return cls.model_validate(dict(data))
        except ValidationError as exc:
            raise ConfigError(format_validation_error(exc, "config")) from exc

    def with_overrides(self, overrides: Mapping[str, Any]) -> Config:
        """Return a copy with ``overrides`` (dotted-free, nested dict) merged in."""
        merged = deep_merge(self.model_dump(mode="python"), dict(overrides))
        return Config.from_dict(merged)

    def to_stored_dict(self) -> dict[str, Any]:
        """Dict for the database, with auth values replaced by ``***``."""
        data = self.model_dump(mode="python")
        auth = data.get("auth") or {}
        data["auth"] = {
            "headers": sorted(auth.get("headers") or {}),
            "cookies": sorted(auth.get("cookies") or {}),
            "redacted": True,
        }
        return data

    def seed_urls(self) -> list[str]:
        return [self.site] if self.site else []

    def resolved_database(self, override: str | Path | None = None) -> Path:
        return database_path(override if override is not None else self.database)

    def safe_headers(self) -> dict[str, str]:
        """Headers to send, with ``env:`` references resolved (never for display).

        An entry whose ``env:VAR`` is not set is dropped rather than sent empty: a blank
        ``Authorization`` header is worse than none (some servers answer 400), and the
        crawler warns about it instead.
        """
        from linkkeep.fetcher import resolve_env_value

        headers = {
            name: value
            for name, value in (
                (n, resolve_env_value(str(v))) for n, v in self.auth.headers.items()
            )
            if value
        }
        headers.setdefault("User-Agent", self.user_agent)
        return headers

    def safe_cookies(self) -> dict[str, str]:
        from linkkeep.fetcher import resolve_env_value

        return {
            name: value
            for name, value in (
                (n, resolve_env_value(str(v))) for n, v in self.auth.cookies.items()
            )
            if value
        }

    def unresolved_auth(self) -> list[str]:
        """Human-readable notes about auth entries that resolved to nothing.

        The value is never included - only the name and the variable it came from.
        """
        from linkkeep.fetcher import resolve_env_value

        notes: list[str] = []
        for kind, mapping in (("header", self.auth.headers), ("cookie", self.auth.cookies)):
            for name, raw in mapping.items():
                value = str(raw)
                if resolve_env_value(value):
                    continue
                if value.startswith("env:"):
                    notes.append(f"{kind} {name}: env:{value[4:].strip()} is not set")
                else:
                    notes.append(f"{kind} {name}: value is empty")
        return notes

    def url_filter(self) -> UrlFilter:
        return UrlFilter(include=self.rules.include, exclude=self.rules.exclude)


def format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"Invalid configuration in {source}:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def read_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping, got {type(raw).__name__}")
    return raw


def prune_nones(data: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values at every depth.

    CLI flags arrive as ``None`` when the user did not pass them, and nested sections must
    not overwrite a config file value with nothing.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        out[key] = prune_nones(value) if isinstance(value, Mapping) else value
    return out


def deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``extra`` over ``base`` (``extra`` wins)."""
    out: dict[str, Any] = dict(base)
    for key, value in extra.items():
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = deep_merge(current, value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- paths
def data_dir() -> Path:
    """Platform-appropriate user data directory (``LINKKEEP_HOME`` wins)."""
    env = os.environ.get("LINKKEEP_HOME")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if os.name == "nt":  # pragma: no cover - Windows path
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return base / "linkkeep"
    if sys.platform == "darwin":  # pragma: no cover - macOS path
        return home / "Library" / "Application Support" / "linkkeep"
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return xdg / "linkkeep"


def database_path(override: str | Path | None = None) -> Path:
    path = Path(override).expanduser() if override else data_dir() / "linkkeep.sqlite"
    if path.is_dir():
        path = path / "linkkeep.sqlite"
    return path


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def find_config_file(start: Path | None = None, explicit: str | Path | None = None) -> Path | None:
    """Locate a config file: explicit flag, then cwd upwards, then the data dir."""
    if explicit is not None:
        return Path(explicit).expanduser()
    directory = (start or Path.cwd()).resolve()
    for candidate in [directory, *directory.parents]:
        for name in CONFIG_FILENAMES:
            found = candidate / name
            if found.is_file():
                return found
    for name in CONFIG_FILENAMES:
        found = data_dir() / name
        if found.is_file():
            return found
    return None


# --------------------------------------------------------------------------- filters
class UrlFilter:
    """Include/exclude matcher.

    A pattern is used as a regular expression when it compiles as one, otherwise as a
    glob.  ``re:`` and ``glob:`` prefixes force the interpretation.  Regex patterns are
    matched with ``search``; globs must match the whole URL.
    """

    def __init__(self, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> None:
        self.include = [compile_pattern(p) for p in include]
        self.exclude = [compile_pattern(p) for p in exclude]

    def allows(self, url: str) -> bool:
        if self.exclude and any(rx.search(url) for rx in self.exclude):
            return False
        if self.include:
            return any(rx.search(url) for rx in self.include)
        return True

    @property
    def active(self) -> bool:
        return bool(self.include or self.exclude)

    def describe(self) -> str:
        parts = [f"include={len(self.include)}", f"exclude={len(self.exclude)}"]
        return ", ".join(parts)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a user-supplied URL pattern.

    ``re:``/``glob:`` force an interpretation.  Otherwise the pattern is used as a
    regular expression when it compiles as one (matched with ``search``) and as a glob
    when it does not (``*.pdf`` and ``*/logout*`` are not valid regexes); globs are
    matched against the whole URL.
    """
    text = pattern.strip()
    if not text:
        msg = "empty URL pattern"
        raise ConfigError(msg)
    forced: str | None = None
    for prefix, kind in (("re:", "re"), ("glob:", "glob"), ("regex:", "re")):
        if text.lower().startswith(prefix):
            forced = kind
            text = text[len(prefix) :]
            break
    if forced == "glob":
        return _compile_glob(text, pattern, None)
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error as exc:
        if forced == "re":
            msg = f"invalid URL pattern {pattern!r}: {exc}"
            raise ConfigError(msg) from exc
        return _compile_glob(text, pattern, exc)


def _compile_glob(text: str, original: str, cause: BaseException | None) -> re.Pattern[str]:
    try:
        return re.compile(glob_to_regex(text), re.IGNORECASE)
    except re.error as exc:
        reason = f" (as a regular expression: {cause})" if cause is not None else ""
        msg = f"invalid URL pattern {original!r}: {exc}{reason}"
        raise ConfigError(msg) from exc


# --------------------------------------------------------------------------- template
INIT_CONFIG_YAML = """\
# Linkkeep configuration - https://github.com/YOUR-OWNER/linkkeep
# Every key is optional; CLI flags always win over this file.
# Structure: crawl (engine limits), rules (what counts as internal and what to
# skip), detection (thresholds), auth (secrets), output, watch.

# Default seed URL, so `linkkeep crawl` works without arguments.
# site: https://example.com

# Sent as User-Agent. Keep the URL so site owners can contact you.
user_agent: "Linkkeep/0.1 (+https://github.com/YOUR-OWNER/linkkeep)"

# SQLite history file. Defaults to the platform data directory
# (~/.local/share/linkkeep/linkkeep.sqlite on Linux); LINKKEEP_HOME also works.
# database: ./linkkeep.sqlite

# Wait for the Crawl-delay value in robots.txt when present.
respect_crawl_delay: true

crawl:
  max_pages: 1000            # internal pages fetched per run
  # max_depth: 3             # unlimited when omitted; 0 = seeds only
  concurrency: 10            # simultaneous requests
  rate: 5                    # requests per second, per host
  timeout: 15                # seconds per request
  retries: 1                 # retries for timeouts / 5xx / 429
  max_redirect_hops: 3       # more hops than this -> redirect_chain issue
  max_response_size: 2000000 # only parse HTML bodies up to this many bytes

rules:
  include_subdomains: false  # subdomains of the seed host count as internal
  # Patterns are regex when they parse as one, otherwise globs. Prefix with
  # "re:" or "glob:" to force. Include wins first, then exclude removes.
  include: []
  # include:
  #   - "glob:https://example.com/blog/**"
  exclude:
    - 're:\\.(pdf|zip|jpg|jpeg|png|gif|webp|svg|ico|css|js|woff2?)$'
    - "glob:*/logout*"
    - "glob:*/search*"
  # Status codes never reported (LinkedIn answers 999 to bots).
  ignore_status: [999]
  check_fragments: false     # verify #anchors exist on the target page
  sitemap: false             # also queue URLs from sitemap.xml
  follow_external: true      # status-check links pointing off-site
  allow_private: false       # refuse loopback/private IPs unless set
  ignore_robots: false       # crawl anyway (prints a warning)
  url_trailing_slash: keep   # keep | strip | add (dedupe policy only)
  url_sort_query: false      # sort query params before dedupe

detection:
  slow_ms: 5000              # `slow` info issue above this response time
  mixed_content: true        # http resources linked from https pages
  temporary_redirect_hint: true # 302/307 on internal links -> info

auth:
  # Values are read when sending the request and never printed, logged or stored.
  # "env:NAME" reads the environment variable NAME.
  headers: {}
  # headers:
  #   Authorization: "env:LINKKEEP_AUTH"
  cookies: {}
  # cookies:
  #   session: "env:LINKKEEP_SESSION"

output:
  fail_on: error             # error | warning | info | never
  format: text               # text | json | markdown | html | junit | none
  color: true
  quiet: false
  verbose: false

watch:
  every: 6h                  # interval for `linkkeep watch`
  notify_webhook: null       # POST a JSON summary when NEW issues appear
"""


def parse_duration(text: str) -> float:
    """``"90d"``/``"6h"``/``"30m"``/``"45s"``/``"1w"`` -> seconds."""
    value = str(text).strip().lower()
    match = re.fullmatch(
        r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?:in)?)?(?:(\d+(?:\.\d+)?)s?)?", value
    )
    if match is None or not any(match.groups()):
        # Plain number of seconds, or a full ISO-ish "0.5h".
        fallback = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw]?)", value)
        if fallback is None:
            raise ConfigError(f"invalid duration: {text!r} (use e.g. 45s, 30m, 6h, 90d)")
        amount, unit = float(fallback.group(1)), fallback.group(2) or "s"
        return amount * _UNIT_SECONDS[unit]
    weeks, days, hours, minutes, seconds = match.groups()
    total = 0.0
    total += int(weeks or 0) * 7 * 86400
    total += int(days or 0) * 86400
    total += int(hours or 0) * 3600
    total += int(minutes or 0) * 60
    total += float(seconds or 0)
    return total


_UNIT_SECONDS: Mapping[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
