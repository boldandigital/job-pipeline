"""Base class for public-API job discovery sources.

A discovery source has a stable ``name`` (matches the jobs.source column),
an HTTP fetch method that pulls raw JSON from a platform endpoint, and a
parse method that maps one raw record to the canonical job dict.

We deliberately keep the network call (``fetch_jobs``) separate from the
per-row mapping (``parse``) so tests can exercise the parser with fake JSON
without standing up a mock HTTP server.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseDiscoverySource(ABC):
    """Contract every public-API discovery source implements."""

    name: str  # "greenhouse" / "lever" / "ashby" — matches jobs.source

    @abstractmethod
    def fetch_jobs(self, companies: List[str]) -> List[Dict[str, Any]]:
        """Return raw job dicts from the platform's public endpoint.

        ``companies`` is a list of platform-specific slugs (e.g. for
        Greenhouse, "stripe", "verkada", "linear"). Implementations should
        be polite: 1 req/sec, 10s timeout, 3 retries on network errors.
        Returns a flat list of raw JSON records (one per job).
        """

    @abstractmethod
    def parse(self, raw_job: Dict[str, Any], company: str) -> Dict[str, Any]:
        """Map a single raw record to the canonical job dict.

        Output shape (all keys are strings, description is HTML or plain):

            {
                "title":       str,
                "company":     str,    # human-friendly display name
                "location":    str,    # free text — "Berlin, DE" / "Remote"
                "url":         str,    # canonical apply/posting URL (dedup key)
                "source":      str,    # self.name
                "description": str,    # HTML or plain — kept as-is for the renderer
                "salary_text": str?,   # optional, only when the platform exposes it
                "posted_at":   str?,   # optional, ISO-ish string
            }
        """


__all__ = ["BaseDiscoverySource"]
