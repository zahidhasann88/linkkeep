"""Every detection rule, in isolation."""

from __future__ import annotations

import pytest

from linkkeep.fetcher import FetchError, FetchResult
from linkkeep.issues import (
    DETECTORS,
    DetectionSettings,
    LinkContext,
    build_issue,
    detect_broken,
    detect_missing_fragment,
    detect_mixed_content,
    detect_redirect_problems,
    detect_robots_block,
    detect_slow,
    detect_unreachable,
    error_reason,
    run_link_detectors,
    severity_at_least,
    summarise,
)
from linkkeep.models import (
    DESCRIPTION_BY_TYPE,
    SEVERITY_BY_TYPE,
    IssueType,
    RedirectHop,
    Severity,
    make_fingerprint,
)

SOURCE = "https://site.test/blog"
TARGET = "https://site.test/old-post"


def result(
    *,
    status: int | None = 200,
    final: str | None = None,
    chain: list[tuple[str, int]] | None = None,
    error: tuple[str, str] | None = None,
    duration_ms: int = 12,
) -> FetchResult:
    hops = [RedirectHop(url=url, status=code, location=None) for url, code in (chain or [])]
    return FetchResult(
        url=TARGET,
        method="GET",
        status=status,
        final_url=final or TARGET,
        chain=[*hops, RedirectHop(url=final or TARGET, status=status)],
        error=None if error is None else FetchError(*error),
        duration_ms=duration_ms,
    )


def ctx(**kwargs: object) -> LinkContext:
    extra = dict(kwargs)
    extra.setdefault("source_url", SOURCE)
    extra.setdefault("target_url", TARGET)
    extra.setdefault("internal", True)
    extra.setdefault("element", "a")
    extra.setdefault("attribute", "href")
    extra.setdefault("link_text", "Old post")
    if "result" not in extra:
        extra["result"] = result()
    return LinkContext(**extra)  # type: ignore[arg-type]


def settings(**kwargs: object) -> DetectionSettings:
    return DetectionSettings(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------- broken
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451, 500, 503])
def test_error_statuses_are_broken(status: int) -> None:
    issues = detect_broken(ctx(result=result(status=status)), settings())
    assert len(issues) == 1
    assert issues[0].type is IssueType.BROKEN
    assert issues[0].severity is Severity.ERROR
    assert issues[0].status == status
    assert issues[0].source_url == SOURCE
    assert issues[0].target_url == TARGET


@pytest.mark.parametrize("status", [200, 201, 204, 301, 302, 304])
def test_ok_statuses_are_not_broken(status: int) -> None:
    assert detect_broken(ctx(result=result(status=status)), settings()) == []


def test_ignore_status_silences_known_flaky_codes() -> None:
    issue_ctx = ctx(result=result(status=999))
    assert len(detect_broken(issue_ctx, settings())) == 1
    assert detect_broken(issue_ctx, settings(ignore_status=frozenset({999}))) == []


def test_ignore_status_also_silences_redirect_endpoints() -> None:
    issue_ctx = ctx(result=result(status=999, final="https://other.test/x", chain=[(TARGET, 301)]))
    assert detect_broken(issue_ctx, settings(ignore_status=frozenset({999}))) == []


def test_broken_records_the_element_that_carried_the_link() -> None:
    issues = detect_broken(
        ctx(result=result(status=404), element="img", attribute="src"), settings()
    )
    assert issues[0].element == "img"
    assert issues[0].attribute == "src"
    assert issues[0].link_text == "Old post"


# ---------------------------------------------------------------- unreachable
@pytest.mark.parametrize(
    ("kind", "detail", "expected"),
    [
        ("dns", "[Errno -2] Name or service not known", "DNS failure"),
        ("connect", "[Errno 111] Connection refused", "connection refused"),
        ("tls", "SSLCertVerificationError: certificate verify failed", "TLS error"),
        ("timeout", "timed out", "timed out"),
    ],
)
def test_unreachable_reasons(kind: str, detail: str, expected: str) -> None:
    issues = detect_unreachable(ctx(result=result(status=None, error=(kind, detail))), settings())
    assert len(issues) == 1
    issue = issues[0]
    assert issue.type is IssueType.UNREACHABLE
    assert issue.severity is Severity.ERROR
    assert issue.status is None
    assert issue.reason is not None
    assert issue.reason.startswith(expected)


def test_too_large_is_not_reported_as_a_link_problem() -> None:
    issues = detect_unreachable(
        ctx(result=result(status=None, error=("too_large", "body exceeded limit"))), settings()
    )
    assert issues == []


def test_error_reason_truncates_long_details() -> None:
    text = error_reason("dns", "x" * 500)
    assert len(text) < 200
    assert text.startswith("DNS failure")


def test_error_reason_does_not_duplicate_the_label() -> None:
    assert error_reason("timeout", "timed out") == "timed out"


# ------------------------------------------------------------------ redirects
def test_long_chain_is_a_warning() -> None:
    chain = [(f"https://site.test/h{i}", 301) for i in range(5)]
    issues = detect_redirect_problems(
        ctx(result=result(status=200, final="https://site.test/final", chain=chain)),
        settings(max_redirect_hops=3),
    )
    codes = [issue.type for issue in issues]
    assert IssueType.REDIRECT_CHAIN in codes
    chain_issue = next(i for i in issues if i.type is IssueType.REDIRECT_CHAIN)
    assert chain_issue.severity is Severity.WARNING
    assert "more than the limit of 3" in (chain_issue.reason or "")
    assert len(chain_issue.redirect_chain) == 6


def test_short_chain_is_quiet() -> None:
    issues = detect_redirect_problems(
        ctx(result=result(status=200, final="https://site.test/final", chain=[(TARGET, 301)])),
        settings(max_redirect_hops=3),
    )
    assert issues == []


def test_chain_ending_in_error_is_redirect_to_error() -> None:
    issues = detect_broken(
        ctx(result=result(status=404, final="https://site.test/gone", chain=[(TARGET, 301)])),
        settings(),
    )
    assert len(issues) == 1
    assert issues[0].type is IssueType.REDIRECT_TO_ERROR
    assert issues[0].status == 404
    assert issues[0].reason == "redirect chain ends in HTTP 404"
    assert issues[0].final_url == "https://site.test/gone"


def test_http_downgrade_is_a_warning() -> None:
    issue_ctx = ctx(
        target_url="https://site.test/secure",
        result=result(
            status=200, final="http://site.test/insecure", chain=[("https://site.test/secure", 301)]
        ),
    )
    issues = detect_redirect_problems(issue_ctx, settings())
    assert [issue.type for issue in issues] == [IssueType.REDIRECT_HTTP_DOWNGRADE]
    assert issues[0].severity is Severity.WARNING


def test_https_to_https_redirect_is_not_a_downgrade() -> None:
    issue_ctx = ctx(
        target_url="https://site.test/a",
        result=result(
            status=200, final="https://site.test/b", chain=[("https://site.test/a", 301)]
        ),
    )
    assert detect_redirect_problems(issue_ctx, settings()) == []


def test_temporary_redirect_on_internal_page_is_an_info_hint() -> None:
    issue_ctx = ctx(
        result=result(
            status=200, final="https://site.test/new", chain=[(TARGET, 302)], duration_ms=5
        ),
    )
    issues = detect_redirect_problems(issue_ctx, settings())
    assert [issue.type for issue in issues] == [IssueType.REDIRECT_TEMPORARY_PERMANENT_INTERNAL]
    assert issues[0].severity is Severity.INFO
    assert "301" in (issues[0].reason or "")


def test_permanent_redirect_gets_no_hint() -> None:
    issue_ctx = ctx(result=result(status=200, final="https://site.test/new", chain=[(TARGET, 301)]))
    assert detect_redirect_problems(issue_ctx, settings()) == []


def test_hint_skipped_for_external_links_and_when_disabled() -> None:
    issue_ctx = ctx(
        internal=False,
        result=result(status=200, final="https://other.test/new", chain=[(TARGET, 302)]),
    )
    assert detect_redirect_problems(issue_ctx, settings()) == []
    internal = ctx(result=result(status=200, final="https://site.test/new", chain=[(TARGET, 302)]))
    assert detect_redirect_problems(internal, settings(report_temporary_redirects=False)) == []


def test_redirect_loop_error_becomes_an_issue() -> None:
    issues = detect_unreachable(
        ctx(
            result=result(
                status=301, error=("redirect_loop", "redirect loop back to https://site.test/a")
            )
        ),
        settings(),
    )
    assert issues[0].type is IssueType.REDIRECT_LOOP
    assert issues[0].severity is Severity.ERROR


def test_redirect_limit_error_is_reported_as_a_chain() -> None:
    issues = detect_unreachable(
        ctx(
            result=result(
                status=301, error=("redirect_limit", "more than 3 redirects (4 hops recorded)")
            )
        ),
        settings(),
    )
    assert issues[0].type is IssueType.REDIRECT_CHAIN
    assert "more than 3 redirects" in (issues[0].reason or "")


# -------------------------------------------------------------- mixed content
def test_mixed_content_flagged_on_https_pages() -> None:
    issue_ctx = ctx(
        source_url="https://site.test/",
        target_url="http://cdn.test/banner.png",
        element="img",
        result=result(status=None),
    )
    issues = detect_mixed_content(issue_ctx, settings())
    assert len(issues) == 1
    assert issues[0].type is IssueType.MIXED_CONTENT
    assert issues[0].severity is Severity.WARNING


def test_mixed_content_ignored_for_http_pages_and_https_targets() -> None:
    http_page = ctx(source_url="http://site.test/", target_url="http://cdn.test/x.png")
    https_target = ctx(source_url="https://site.test/", target_url="https://cdn.test/x.png")
    disabled = ctx(source_url="https://site.test/", target_url="http://cdn.test/x.png")
    assert detect_mixed_content(http_page, settings()) == []
    assert detect_mixed_content(https_target, settings()) == []
    assert detect_mixed_content(disabled, settings(report_mixed_content=False)) == []


# ------------------------------------------------------------------------- slow
def test_slow_is_informational() -> None:
    issues = detect_slow(ctx(result=result(duration_ms=5001)), settings(slow_ms=5000))
    assert len(issues) == 1
    assert issues[0].type is IssueType.SLOW
    assert issues[0].severity is Severity.INFO
    assert "5001 ms" in (issues[0].reason or "")


def test_fast_and_failed_responses_are_not_slow() -> None:
    assert detect_slow(ctx(result=result(duration_ms=5000)), settings(slow_ms=5000)) == []
    assert (
        detect_slow(
            ctx(result=result(duration_ms=99999, error=("timeout", "timed out"))), settings()
        )
        == []
    )


# --------------------------------------------------------------------- anchors
def test_missing_fragment_only_when_the_target_is_known() -> None:
    missing = detect_missing_fragment(
        source_url=SOURCE, target_url=TARGET, fragment="nope", anchors={"a", "b"}
    )
    assert missing is not None
    assert missing.type is IssueType.MISSING_FRAGMENT
    assert missing.target_url == f"{TARGET}#nope"
    assert missing.final_url == TARGET

    assert (
        detect_missing_fragment(source_url=SOURCE, target_url=TARGET, fragment="a", anchors={"a"})
        is None
    )
    assert (
        detect_missing_fragment(source_url=SOURCE, target_url=TARGET, fragment="a", anchors=None)
        is None
    )
    assert (
        detect_missing_fragment(source_url=SOURCE, target_url=TARGET, fragment="", anchors={"a"})
        is None
    )


def test_robots_block_issue() -> None:
    issue = detect_robots_block(source_url=SOURCE, target_url="https://site.test/private/x")
    assert issue.type is IssueType.BLOCKED_BY_ROBOTS
    assert issue.severity is Severity.INFO
    assert "robots.txt" in (issue.reason or "")


# --------------------------------------------------------------------- plumbing
def test_fingerprint_is_stable_and_scoped_to_source_and_target() -> None:
    a = build_issue(IssueType.BROKEN, source_url=SOURCE, target_url=TARGET, status=404)
    b = build_issue(IssueType.BROKEN, source_url=SOURCE, target_url=TARGET, status=500)
    assert a.fingerprint == b.fingerprint  # status changes do not create a new issue
    assert a.fingerprint == make_fingerprint(IssueType.BROKEN, SOURCE, TARGET)
    other_source = build_issue(
        IssueType.BROKEN, source_url="https://site.test/other", target_url=TARGET
    )
    assert other_source.fingerprint != a.fingerprint
    other_type = build_issue(IssueType.UNREACHABLE, source_url=SOURCE, target_url=TARGET)
    assert other_type.fingerprint != a.fingerprint


def test_every_issue_type_has_a_severity_and_a_description() -> None:
    for issue_type in IssueType:
        assert issue_type in SEVERITY_BY_TYPE, issue_type
        assert issue_type in DESCRIPTION_BY_TYPE, issue_type
        assert isinstance(SEVERITY_BY_TYPE[issue_type], Severity)


def test_issue_public_dict_keys_are_stable() -> None:
    issue = build_issue(IssueType.BROKEN, source_url=SOURCE, target_url=TARGET, status=404)
    payload = issue.public_dict()
    assert sorted(payload) == sorted(
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
        ]
    )
    assert payload["code"] == "broken"
    assert payload["found_at"].startswith("20")


def test_run_link_detectors_deduplicates_and_orders() -> None:
    issue_ctx = ctx(
        target_url="https://site.test/a",
        result=result(
            status=500,
            final="http://site.test/b",
            chain=[(f"https://site.test/h{i}", 301) for i in range(5)],
        ),
        source_url="https://site.test/",
    )
    issues = run_link_detectors(issue_ctx, settings(max_redirect_hops=3))
    codes = [issue.code for issue in issues]
    assert len(codes) == len(set(codes)), "fingerprints must de-duplicate"
    assert codes[0] == "redirect_to_error"  # errors first, then warnings
    assert set(codes) == {"redirect_to_error", "redirect_chain", "redirect_http_downgrade"}


def test_detector_registry_is_exposed_for_extensions() -> None:
    assert callable(DETECTORS[0])
    assert len(DETECTORS) >= 5


def test_custom_detector_can_be_injected() -> None:
    def only_always(issue_ctx: LinkContext, conf: DetectionSettings) -> list[object]:
        return [
            build_issue(
                IssueType.SLOW, source_url=issue_ctx.source_url, target_url=issue_ctx.target_url
            )
        ]

    issues = run_link_detectors(ctx(), settings(), detectors=[only_always])
    assert [issue.code for issue in issues] == ["slow"]


@pytest.mark.parametrize(
    ("fail_on", "issue_type", "expected"),
    [
        ("error", IssueType.BROKEN, True),
        ("error", IssueType.MIXED_CONTENT, False),
        ("warning", IssueType.MIXED_CONTENT, True),
        ("warning", IssueType.SLOW, False),
        ("info", IssueType.SLOW, True),
        ("never", IssueType.BROKEN, False),
    ],
)
def test_severity_at_least(fail_on: str, issue_type: IssueType, expected: bool) -> None:
    issue = build_issue(issue_type, source_url=None, target_url=TARGET)
    assert severity_at_least(issue, fail_on) is expected


def test_severity_at_least_rejects_unknown_levels() -> None:
    issue = build_issue(IssueType.BROKEN, source_url=None, target_url=TARGET)
    with pytest.raises(ValueError, match="unknown --fail-on"):
        severity_at_least(issue, "catastrophic")


def test_summarise_groups_counts() -> None:
    issues = [
        build_issue(IssueType.BROKEN, source_url=SOURCE, target_url="https://site.test/a"),
        build_issue(IssueType.BROKEN, source_url=SOURCE, target_url="https://site.test/b"),
        build_issue(IssueType.SLOW, source_url=SOURCE, target_url="https://site.test/c"),
    ]
    assert summarise(issues) == {"broken": {"error": 2}, "slow": {"info": 1}}


def test_issue_label_and_sort_key() -> None:
    with_status = build_issue(IssueType.BROKEN, source_url=SOURCE, target_url=TARGET, status=404)
    assert with_status.label == "HTTP 404"
    with_reason = build_issue(
        IssueType.UNREACHABLE, source_url=SOURCE, target_url=TARGET, reason="DNS failure"
    )
    assert with_reason.label == "DNS failure"
    frag = build_issue(
        IssueType.MISSING_FRAGMENT, source_url=SOURCE, target_url=f"{TARGET}#x", final_url=TARGET
    )
    assert frag.label == "fragment missing on target page"
    assert (
        with_status.sort_key() < with_reason.sort_key()
    )  # errors sort before errors by code, then info
