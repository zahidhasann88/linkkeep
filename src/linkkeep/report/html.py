"""Self-contained HTML report: inline CSS + inline JS, no external assets.

* one table row per issue, with ``data-type`` / ``data-severity`` attributes
* filter buttons (issue type and severity) wired up by ~30 lines of inline JS
* a text filter box, ``/`` to focus
* readable on a phone: the table collapses into stacked cards under 720px
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Final

from linkkeep.models import Issue, Severity
from linkkeep.report import ReportData, chain_summary, describe_issue, since_text, stamp

SEVERITY_LABEL: dict[Severity, str] = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "info",
}

CSS: Final = """
:root{color-scheme:light dark;--bg:#fff;--fg:#16191d;--muted:#5b6572;--line:#e3e7ec;--card:#f7f8fa;
--error:#b42318;--error-bg:#fee4e2;--warning:#b54708;--warning-bg:#fef0c7;--info:#175cd3;--info-bg:#d1e9ff}
@media (prefers-color-scheme:dark){:root{--bg:#101216;--fg:#e8eaed;--muted:#98a1ae;--line:#252a33;--card:#171a20;
--error:#f97066;--error-bg:#55160c;--warning:#f79009;--warning-bg:#3c2604;--info:#53b1fd;--info-bg:#102a43}}
*{box-sizing:border-box}
body{margin:0;padding:1.25rem;background:var(--bg);color:var(--fg);
font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:1100px;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 .25rem;word-break:break-word}
h2{font-size:1.05rem;margin:1.75rem 0 .5rem}
a{color:var(--info);word-break:break-all}
.muted{color:var(--muted);font-size:.85rem}
.metrics{display:flex;flex-wrap:wrap;gap:.5rem;margin:.9rem 0 0;padding:0;list-style:none}
.metrics li{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.4rem .65rem;font-size:.85rem}
.metrics b{font-size:1.05rem;display:block}
.filters{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:1.1rem 0 .8rem}
.filters button{font:inherit;font-size:.82rem;padding:.3rem .6rem;border-radius:999px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer}
.filters button[aria-pressed=true]{border-color:var(--fg);background:var(--fg);color:var(--bg)}
.filters input{font:inherit;font-size:.85rem;padding:.35rem .55rem;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);min-width:10rem;flex:1}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.55rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
td.url{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.82rem;word-break:break-all}
.tag{display:inline-block;padding:.05rem .45rem;border-radius:999px;font-size:.72rem;font-weight:600;
text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}
.tag.error{color:var(--error);background:var(--error-bg)}
.tag.warning{color:var(--warning);background:var(--warning-bg)}
.tag.info{color:var(--info);background:var(--info-bg)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85em}
.chain{color:var(--muted);font-size:.78rem;margin-top:.2rem;word-break:break-all}
.empty{border:1px dashed var(--line);border-radius:10px;padding:1.25rem;text-align:center;color:var(--muted)}
.cols{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(19rem,1fr))}
.panel{border:1px solid var(--line);border-radius:10px;padding:.75rem .9rem;background:var(--card)}
.panel ul{margin:.4rem 0 0;padding-left:1.1rem}
.panel li{margin:.3rem 0;word-break:break-all}
[hidden]{display:none!important}
@media (max-width:720px){
 body{padding:.75rem}
 thead{display:none}
 tr{display:block;border:1px solid var(--line);border-radius:10px;padding:.35rem .55rem;margin-bottom:.6rem}
 td{display:block;border:0;padding:.2rem 0}
 td[data-label]::before{content:attr(data-label) " ";color:var(--muted);font-size:.72rem;text-transform:uppercase}
}
"""

JS: Final = """
(function () {
  var rows = Array.prototype.slice.call(document.querySelectorAll('tbody tr[data-type]'));
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.filters button[data-filter]'));
  var search = document.getElementById('q');
  var state = { type: 'all', severity: 'all', text: '' };
  function apply() {
    var shown = 0;
    rows.forEach(function (row) {
      var okType = state.type === 'all' || row.getAttribute('data-type') === state.type;
      var okSev = state.severity === 'all' || row.getAttribute('data-severity') === state.severity;
      var okText = !state.text || row.textContent.toLowerCase().indexOf(state.text) !== -1;
      var show = okType && okSev && okText;
      row.hidden = !show;
      if (show) { shown++; }
    });
    var count = document.getElementById('shown');
    if (count) { count.textContent = shown + ' of ' + rows.length + ' shown'; }
    buttons.forEach(function (button) {
      var on = button.getAttribute('data-filter') === 'type' ? state.type === button.getAttribute('data-value')
                                                       : state.severity === button.getAttribute('data-value');
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      var key = button.getAttribute('data-filter') === 'type' ? 'type' : 'severity';
      var value = button.getAttribute('data-value');
      state[key] = state[key] === value ? 'all' : value;
      apply();
    });
  });
  if (search) {
    search.addEventListener('input', function () { state.text = search.value.trim().toLowerCase(); apply(); });
    document.addEventListener('keydown', function (event) {
      if (event.key === '/' && document.activeElement !== search) { event.preventDefault(); search.focus(); }
    });
  }
  apply();
})();
"""


def render(data: ReportData) -> str:
    run = data.run
    counts = data.counts_by_severity
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Linkkeep run #{run.id} - {esc(run.site)}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        _header(data),
        _metrics(data),
        _filters(data),
        _table(data, data.sorted_issues),
        _diff_section(data) if data.diff is not None else "",
        f'<p class="muted">Generated {esc(stamp(data.generated_at))} UTC by Linkkeep {esc(data.tool_version)}. '
        "This file is self-contained: no scripts, styles or fonts are loaded from anywhere else.</p>",
        f"<script>{JS}</script>",
        "</main>",
        "</body>",
        "</html>",
    ]
    _ = counts
    return "\n".join(part for part in parts if part)


def _header(data: ReportData) -> str:
    run = data.run
    status = f'<span class="tag {("error" if run.status.value in {"failed"} else "info")}">{esc(run.status.value)}</span>'
    notes = f'<p class="muted">{esc(run.notes)}</p>' if run.notes else ""
    return (
        f"<h1>Linkkeep &middot; {esc(run.site)}</h1>"
        f'<p class="muted">run #{run.id} {status} &middot; {esc(stamp(run.started_at))} UTC '
        f"&middot; {data.duration_seconds:.1f}s &middot; {esc(', '.join(f'seed: {s}' for s in run.seeds[:3]))}</p>"
        f"{notes}"
    )


def _metrics(data: ReportData) -> str:
    counts = data.run.counts
    by_severity = data.counts_by_severity
    items: list[tuple[str, int]] = [
        ("errors", by_severity[Severity.ERROR]),
        ("warnings", by_severity[Severity.WARNING]),
        ("info", by_severity[Severity.INFO]),
        ("pages", counts.pages),
        ("links", counts.links),
        ("checked", counts.checked),
    ]
    if counts.robots_blocked:
        items.append(("robots blocked", counts.robots_blocked))
    if counts.skipped_scheme:
        items.append(("non-http", counts.skipped_scheme))
    cells = "".join(f"<li><b>{value}</b>{esc(label)}</li>" for label, value in items)
    return f'<ul class="metrics">{cells}</ul>'


def _filters(data: ReportData) -> str:
    types = list(data.issues_by_type)
    buttons = [
        '<button data-filter="severity" data-value="error" aria-pressed="false">errors</button>'
    ]
    buttons.append(
        '<button data-filter="severity" data-value="warning" aria-pressed="false">warnings</button>'
    )
    buttons.append(
        '<button data-filter="severity" data-value="info" aria-pressed="false">info</button>'
    )
    for issue_type in types:
        count = len(data.issues_by_type[issue_type])
        buttons.append(
            f'<button data-filter="type" data-value="{esc(issue_type.value)}" aria-pressed="false">'
            f"{esc(issue_type.value)} ({count})</button>"
        )
    return (
        '<div class="filters">'
        '<input id="q" type="search" placeholder="filter URLs / text (press /)" aria-label="Filter issues">'
        + "".join(buttons)
        + '<span id="shown" class="muted"></span>'
        + "</div>"
    )


def _table(data: ReportData, issues: Sequence[Issue]) -> str:
    if not issues:
        return '<p class="empty">No issues found. Every internal page and checked link responded cleanly.</p>'
    rows: list[str] = []
    for issue in issues:
        severity = SEVERITY_LABEL[issue.severity]
        source = f"<code>{esc(_short(issue.source_url))}</code>" if issue.source_url else "&mdash;"
        element = esc(f"<{issue.element} {issue.attribute}>".strip("<> ")) if issue.element else ""
        text = f" &ldquo;{esc(issue.link_text)}&rdquo;" if issue.link_text else ""
        chain = chain_summary(issue)
        rows.append(
            f'<tr data-type="{esc(issue.code)}" data-severity="{severity}">'
            f'<td data-label="severity"><span class="tag {severity}">{severity}</span></td>'
            f'<td data-label="type"><code>{esc(issue.code)}</code></td>'
            f'<td class="url" data-label="target">{esc(issue.target_url)}'
            + (f'<div class="chain">{esc(chain)}</div>' if chain else "")
            + "</td>"
            + f'<td data-label="result">{esc(describe_issue(issue))}</td>'
            + f'<td data-label="found on">{source}{f"<br><span class=muted>{element}{text}</span>" if element or text else ""}</td>'
            + "</tr>"
        )
    return (
        "<table><thead><tr><th>severity</th><th>type</th><th>target</th><th>result</th><th>found on</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _diff_section(data: ReportData) -> str:
    diff = data.diff
    assert diff is not None
    new = _panel(f"New ({len(diff.new)})", diff.new)
    fixed = _panel(f"Fixed ({len(diff.fixed)})", diff.fixed)
    persisting_items = "".join(
        f"<li><code>{esc(item.issue.code)}</code> {esc(_short(item.issue.target_url))} "
        f'<span class="muted">{esc(since_text(item.since, now=data.generated_at))}</span></li>'
        for item in diff.persisting
    )
    persisting = f'<div class="panel"><h2>Persisting ({len(diff.persisting)})</h2><ul>{persisting_items}</ul></div>'
    header = (
        f"<h2>Changes since run #{diff.run_a.id}</h2>"
        f'<p class="muted">older crawl {esc(stamp(diff.run_a.started_at))} UTC &middot; newer crawl '
        f"{esc(stamp(diff.run_b.started_at))} UTC</p>"
    )
    return header + f'<div class="cols">{new}{fixed}{persisting}</div>'


def _panel(title: str, issues: Sequence[Issue]) -> str:
    if not issues:
        return f'<div class="panel"><h2>{esc(title)}</h2><p class="muted">none</p></div>'
    items = "".join(
        f'<li><span class="tag {SEVERITY_LABEL[issue.severity]}">{issue.severity.value}</span> '
        f"<code>{esc(issue.code)}</code> {esc(_short(issue.target_url))}</li>"
        for issue in issues
    )
    return f'<div class="panel"><h2>{esc(title)}</h2><ul>{items}</ul></div>'


def _short(url: str | None, limit: int = 64) -> str:
    if not url:
        return ""
    return url if len(url) <= limit else url[: limit - 1] + "\u2026"


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
