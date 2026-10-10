"""Linkkeep - crawl a site, find broken links, keep a history."""

from linkkeep.models import Issue, IssueType, Severity

__all__ = ["Issue", "IssueType", "Severity", "__version__"]

__version__ = "0.1.0"
