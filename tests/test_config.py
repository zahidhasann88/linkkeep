"""Config loading, precedence, URL patterns, secrets and duration parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from linkkeep.config import (
    INIT_CONFIG_YAML,
    Config,
    ConfigError,
    LinkkeepError,
    UrlFilter,
    compile_pattern,
    data_dir,
    database_path,
    deep_merge,
    find_config_file,
    format_validation_error,
    parse_duration,
    read_yaml_config,
)

YAML = """
site: https://example.org
user_agent: "MyCrawler/1.0"
crawl:
  max_pages: 12
  rate: 2.5
  max_depth: 2
rules:
  include: ["glob:https://example.org/blog/**"]
  exclude: ["re:\\\\.(pdf|zip)$"]
  ignore_status: [999, 429]
  check_fragments: true
detection:
  slow_ms: 800
auth:
  headers:
    Authorization: "env:LINKKEEP_TEST_TOKEN"
output:
  fail_on: warning
  format: markdown
"""


def write_config(tmp_path: Path, text: str = YAML) -> Path:
    path = tmp_path / "linkkeep.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------- loading
def test_defaults_without_a_file() -> None:
    config = Config.load(None)
    assert config.crawl.max_pages == 1000
    assert config.crawl.concurrency == 10
    assert config.crawl.timeout == 15.0
    assert config.crawl.max_redirect_hops == 3
    assert config.rules.include_subdomains is False
    assert config.rules.ignore_robots is False
    assert config.rules.allow_private is False
    assert config.output.fail_on == "error"
    assert config.user_agent.startswith("Linkkeep/")
    assert "http" in config.user_agent


def test_file_values_are_used(tmp_path: Path) -> None:
    config = Config.load(write_config(tmp_path))
    assert config.site == "https://example.org"
    assert config.user_agent == "MyCrawler/1.0"
    assert config.crawl.max_pages == 12
    assert config.crawl.rate == 2.5
    assert config.crawl.max_depth == 2
    assert config.rules.ignore_status == [999, 429]
    assert config.rules.check_fragments is True
    assert config.detection.slow_ms == 800
    assert config.output.fail_on == "warning"
    assert config.output.format == "markdown"


def test_cli_overrides_beat_the_file(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    config = Config.load(
        path, overrides={"crawl": {"max_pages": 5}, "output": {"fail_on": "never"}}
    )
    assert config.crawl.max_pages == 5
    assert config.output.fail_on == "never"
    assert config.crawl.rate == 2.5  # untouched values survive


def test_missing_file_is_a_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        read_yaml_config(tmp_path / "nope.yaml")


def test_broken_yaml_is_a_friendly_error(tmp_path: Path) -> None:
    path = tmp_path / "linkkeep.yaml"
    path.write_text("site: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="Could not parse"):
        read_yaml_config(path)


def test_top_level_list_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "linkkeep.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must contain a YAML mapping"):
        read_yaml_config(path)


def test_empty_file_uses_defaults(tmp_path: Path) -> None:
    path = tmp_path / "linkkeep.yaml"
    path.write_text("# nothing here\n", encoding="utf-8")
    assert Config.load(path).crawl.max_pages == 1000


def test_unknown_key_is_reported_with_a_pointer(tmp_path: Path) -> None:
    path = tmp_path / "linkkeep.yaml"
    path.write_text("crawl:\n  max_page: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        Config.load(path)
    assert "crawl.max_page" in str(excinfo.value)


def test_bad_value_reports_the_field(tmp_path: Path) -> None:
    path = tmp_path / "linkkeep.yaml"
    path.write_text("crawl:\n  concurrency: lots\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        Config.load(path)
    assert "crawl.concurrency" in str(excinfo.value)


def test_range_validation() -> None:
    with pytest.raises(ConfigError, match="greater than"):
        Config.from_dict({"crawl": {"concurrency": 0}})
    with pytest.raises(ConfigError, match="less than"):
        Config.from_dict({"crawl": {"concurrency": 5000}})


def test_format_validation_error_helper() -> None:
    with pytest.raises(ConfigError, match=r"output\.fail_on"):
        Config.from_dict({"output": {"fail_on": "sometimes"}})


def test_format_validation_error_without_path() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        Config(site=1)  # type: ignore[arg-type]
    assert format_validation_error(excinfo.value, "options").startswith(
        "Invalid configuration in options"
    )


# --------------------------------------------------------------------- template
def test_init_template_is_valid_and_parses(tmp_path: Path) -> None:
    path = tmp_path / "linkkeep.yaml"
    path.write_text(INIT_CONFIG_YAML, encoding="utf-8")
    config = Config.load(path)
    assert config.rules.ignore_status == [999]
    assert config.crawl.max_pages == 1000
    assert config.watch.every == "6h"
    assert config.rules.exclude  # the commented examples are real regexes
    assert config.site is None


def test_template_documented_keys_all_exist() -> None:
    """Every top-level key in the template must be a real Config field."""
    import yaml

    data = yaml.safe_load(INIT_CONFIG_YAML)
    fields = set(Config.model_fields)
    assert set(data) <= fields, set(data) - fields


def test_every_section_is_documented_in_the_template() -> None:
    import yaml

    data = yaml.safe_load(INIT_CONFIG_YAML)
    for name, field in Config.model_fields.items():
        annotation = str(field.annotation)
        if annotation.startswith("<class 'linkkeep.config.") and annotation.endswith("Section'>"):
            assert name in data, f"{name} section missing from the init template"


# --------------------------------------------------------------------- database
def test_linkkeep_home_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LINKKEEP_HOME", str(tmp_path / "home"))
    assert data_dir() == tmp_path / "home"
    assert database_path() == tmp_path / "home" / "linkkeep.sqlite"


def test_database_override_and_directory_expansion(tmp_path: Path) -> None:
    assert database_path(tmp_path / "custom.sqlite") == tmp_path / "custom.sqlite"
    assert database_path(tmp_path) == tmp_path / "linkkeep.sqlite"


def test_config_database_is_resolved(tmp_path: Path) -> None:
    config = Config.from_dict({"database": str(tmp_path / "x.sqlite")})
    assert config.resolved_database() == tmp_path / "x.sqlite"
    assert config.resolved_database(tmp_path / "y.sqlite") == tmp_path / "y.sqlite"


def test_config_file_discovery_walks_up_parents(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / "a" / "linkkeep.yml").write_text("site: https://x.test\n", encoding="utf-8")
    found = find_config_file(nested)
    assert found == tmp_path / "a" / "linkkeep.yml"


def test_explicit_config_path_beats_discovery(tmp_path: Path) -> None:
    (tmp_path / "linkkeep.yaml").write_text("site: https://a.test\n", encoding="utf-8")
    other = tmp_path / "other.yaml"
    other.write_text("site: https://b.test\n", encoding="utf-8")
    assert find_config_file(tmp_path, explicit=other) == other


def test_no_config_file_found_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINKKEEP_HOME", str(tmp_path / "empty-home"))
    assert find_config_file(tmp_path / "missing", explicit=None) is None


# ---------------------------------------------------------------------- secrets
def test_auth_values_are_never_stored_or_printed() -> None:
    config = Config.from_dict({"auth": {"headers": {"Authorization": "Bearer super-secret"}}})
    stored = config.to_stored_dict()
    text = repr(stored)
    assert "super-secret" not in text
    assert stored["auth"]["headers"] == ["Authorization"]
    assert stored["auth"]["redacted"] is True


def test_env_values_are_resolved_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINKKEEP_TEST_TOKEN", "tok-123")
    config = Config.from_dict({"auth": {"headers": {"Authorization": "env:LINKKEEP_TEST_TOKEN"}}})
    assert config.safe_headers()["Authorization"] == "tok-123"
    assert "tok-123" not in repr(config.to_stored_dict())
    # an unset variable drops the header instead of sending it blank, and says why
    monkeypatch.delenv("LINKKEEP_TEST_TOKEN")
    assert "Authorization" not in config.safe_headers()
    assert config.safe_headers()["User-Agent"].startswith("Linkkeep/")
    assert config.unresolved_auth() == ["header Authorization: env:LINKKEEP_TEST_TOKEN is not set"]
    assert "tok-123" not in str(config.unresolved_auth())


def test_yaml_file_secrets_are_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINKKEEP_TEST_TOKEN", "from-env")
    path = write_config(tmp_path)
    config = Config.load(path)
    assert config.safe_headers()["Authorization"] == "from-env"
    assert "from-env" not in str(config.to_stored_dict())


def test_empty_literal_auth_values_are_dropped_and_reported() -> None:
    config = Config.from_dict(
        {"auth": {"headers": {"X-Key": ""}, "cookies": {"sid": "env:MISSING_X"}}}
    )
    assert "X-Key" not in config.safe_headers()
    assert config.safe_cookies() == {}
    notes = config.unresolved_auth()
    assert "header X-Key: value is empty" in notes
    assert any(note.startswith("cookie sid: env:MISSING_X") for note in notes)


def test_user_agent_is_always_present() -> None:
    config = Config.from_dict({"auth": {"headers": {"X-Trace": "1"}}})
    assert config.safe_headers()["User-Agent"].startswith("Linkkeep/")


# ---------------------------------------------------------------------- filters
def test_include_and_exclude_globs() -> None:
    urls = [
        "https://site.test/blog/post-1",
        "https://site.test/shop/item",
        "https://site.test/files/a.pdf",
    ]
    include_only = UrlFilter(include=["glob:https://site.test/blog/**"])
    assert [url for url in urls if include_only.allows(url)] == ["https://site.test/blog/post-1"]
    both = UrlFilter(include=["glob:https://site.test/**"], exclude=["glob:*/files/*"])
    assert both.allows(urls[0])
    assert not both.allows(urls[2])
    assert both.active
    assert both.describe().startswith("include=1")


def test_regex_patterns_use_search_semantics() -> None:
    matcher = UrlFilter(exclude=[r"\.pdf$"])
    assert not matcher.allows("https://a.test/x.pdf")
    # the anchor is honoured, so a query string after the suffix keeps the URL allowed
    assert matcher.allows("https://a.test/x.pdf?x=1")
    assert UrlFilter(exclude=["logout"]).allows("https://a.test/account/logout") is False


def test_pattern_prefixes_force_interpretation() -> None:
    assert not UrlFilter(exclude=["re:blog"]).allows("https://a.test/blog")
    assert UrlFilter(exclude=["glob:*/blog"]).allows("https://a.test/blog/x")
    assert not UrlFilter(exclude=["glob:*/blog"]).allows("https://a.test/blog")


def test_invalid_regex_falls_back_to_glob_and_still_matches() -> None:
    matcher = UrlFilter(exclude=["*/*/*/*"])  # not a valid regex (leading *)
    assert not matcher.allows("https://a.test/x/y/z")


def test_compile_pattern_rejects_empty() -> None:
    with pytest.raises(ConfigError, match="empty URL pattern"):
        compile_pattern("   ")


def test_compile_pattern_reports_a_forced_bad_regex() -> None:
    with pytest.raises(ConfigError, match="invalid URL pattern"):
        compile_pattern("re:(unclosed")


def test_no_patterns_allows_everything() -> None:
    matcher = UrlFilter()
    assert matcher.allows("https://any.test/x")
    assert not matcher.active


# ---------------------------------------------------------------------- helpers
def test_deep_merge_is_recursive() -> None:
    merged = deep_merge({"crawl": {"max_pages": 1, "rate": 2}}, {"crawl": {"rate": 5}})
    assert merged == {"crawl": {"max_pages": 1, "rate": 5}}


def test_none_overrides_are_ignored() -> None:
    config = Config.load(None, overrides={"crawl": {"max_pages": None}, "site": None})
    assert config.crawl.max_pages == 1000
    assert config.site is None


def test_with_overrides_keeps_auth_in_memory() -> None:
    config = Config.from_dict({"auth": {"headers": {"A": "secret"}}, "crawl": {"max_pages": 3}})
    updated = config.with_overrides({"crawl": {"max_pages": 9}})
    assert updated.crawl.max_pages == 9
    assert updated.auth.headers["A"] == "secret"


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("45s", 45),
        ("90", 90),
        ("30m", 1800),
        ("1h", 3600),
        ("6h", 21600),
        ("90d", 90 * 86400),
        ("1w", 604800),
        ("1d12h", 129600),
        ("0.5h", 1800),
        ("2h30m", 9000),
    ],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["soon", "", "1x", "abc"])
def test_parse_duration_rejects_nonsense(text: str) -> None:
    with pytest.raises(LinkkeepError, match="invalid duration"):
        parse_duration(text)


def test_config_seed_urls_and_bool_helpers() -> None:
    assert Config.from_dict({"site": "https://a.test"}).seed_urls() == ["https://a.test"]
    assert Config.from_dict({}).seed_urls() == []
    assert isinstance(LinkkeepError("x"), Exception)
