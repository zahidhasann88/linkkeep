"""JSON report - a documented, stable schema.

See ``README.md#json-schema`` for the field-by-field reference.  Keys never change
meaning within a major version; new keys may be added.
"""

from __future__ import annotations

import json
from typing import Any

from linkkeep.models import Issue, LinkRecord, PageRecord
from linkkeep.report import JSON_SCHEMA_VERSION, ReportData, iso


def to_dict(data: ReportData, *, include_details: bool = True) -> dict[str, Any]:
    run = data.run
    payload: dict[str, Any] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool": {"name": "linkkeep", "version": data.tool_version},
        "generated_at": iso(data.generated_at),
        "site": run.site,
        "run": {
            "id": run.id,
            "site": run.site,
            "status": run.status.value,
            "started_at": iso(run.started_at),
            "finished_at": iso(run.finished_at) or None,
            "duration_seconds": round(data.duration_seconds, 3),
            "seeds": list(run.seeds),
            "notes": run.notes,
            "config": run.config,
        },
        "counts": run.counts.model_dump(mode="json"),
        "issues": [_issue(item) for item in data.sorted_issues],
        "diff": _diff(data) if data.diff is not None else None,
    }
    if include_details:
        payload["pages"] = [_page(page) for page in data.pages]
        payload["links"] = [_link(link) for link in data.links]
    return payload


def render(data: ReportData) -> str:
    return json.dumps(to_dict(data), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _issue(issue: Issue) -> dict[str, Any]:
    payload = issue.public_dict()
    payload["description"] = issue.description
    payload["summary"] = issue.label
    return payload


def _page(page: PageRecord) -> dict[str, Any]:
    return {
        "url": page.url,
        "depth": page.depth,
        "status": page.status,
        "final_url": page.final_url,
        "duration_ms": page.duration_ms,
        "content_type": page.content_type,
        "size_bytes": page.size_bytes,
        "error": page.error,
        "crawlable": page.crawlable,
        "links_found": page.links_found,
        "links_checked": page.links_checked,
    }


def _link(link: LinkRecord) -> dict[str, Any]:
    return {
        "source_url": link.source_url,
        "target_url": link.target_url,
        "kind": link.kind.value,
        "element": link.element,
        "attribute": link.attribute,
        "link_text": link.link_text,
        "final_url": link.final_url,
        "status": link.status,
        "duration_ms": link.duration_ms,
        "error": link.error,
        "scheme": link.scheme,
        "checked": link.checked,
        "redirect_chain": [hop.as_dict() for hop in link.redirect_chain],
    }


def _diff(data: ReportData) -> dict[str, Any]:
    diff = data.diff
    assert diff is not None
    return {
        "run_a": {"id": diff.run_a.id, "started_at": iso(diff.run_a.started_at)},
        "run_b": {"id": diff.run_b.id, "started_at": iso(diff.run_b.started_at)},
        "new": [_issue(issue) for issue in diff.new],
        "fixed": [_issue(issue) for issue in diff.fixed],
        "persisting": [
            {
                "since": iso(item.since),
                "runs_seen": item.runs_seen,
                "issue": _issue(item.issue),
            }
            for item in diff.persisting
        ],
        "notes": list(diff.notes),
    }
