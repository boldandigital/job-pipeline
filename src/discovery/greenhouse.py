"""Greenhouse public job board API.

Docs: https://developers.greenhouse.io/job-board.html
Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

The board token is the per-company slug (e.g. ``stripe``, ``verkada``,
``linear``). It is the path segment that follows ``boards.greenhouse.io/``
on the public career page. A 404 means the company does not host its board
on Greenhouse; we silently skip those (no exception, no entry).

The endpoint is unauthenticated and free. ``?content=true`` inlines the
job description body (HTML) so we don't need a second request per job.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import httpx

from .base import BaseDiscoverySource


GREENHOUSE_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseSource(BaseDiscoverySource):
    name = "greenhouse"

    # Politeness — Greenhouse's public board doesn't publish a rate limit,
    # but 1 r/s is comfortably below the noise floor.
    REQUEST_INTERVAL_SEC = 1.0
    TIMEOUT_SEC = 10.0
    MAX_RETRIES = 3

    def fetch_jobs(self, companies: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        client = httpx.Client(
            timeout=self.TIMEOUT_SEC,
            headers={"User-Agent": "job-pipeline/2.1 (discovery; +https://example.local)"},
        )
        try:
            for slug in companies:
                url = f"{GREENHOUSE_BASE}/{slug}/jobs?content=true"
                payload = self._get_with_retry(client, url)
                if payload is None:
                    # Unknown board or transient failure — skip, don't crash the whole run.
                    time.sleep(self.REQUEST_INTERVAL_SEC)
                    continue
                for job in payload.get("jobs", []) or []:
                    job = dict(job)
                    job["_board_token"] = slug  # used by parse() to build the URL
                    out.append(job)
                time.sleep(self.REQUEST_INTERVAL_SEC)
        finally:
            client.close()
        return out

    def _get_with_retry(self, client: httpx.Client, url: str) -> Dict[str, Any] | None:
        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(self.REQUEST_INTERVAL_SEC * attempt)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return None
            if resp.status_code == 404:
                # Board does not exist on Greenhouse — not an error, just nothing to fetch.
                return None
            # 5xx / 429 → retry with backoff
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(self.REQUEST_INTERVAL_SEC * attempt)
                continue
            # Other 4xx — not retriable
            return None
        return None

    def parse(self, raw_job: Dict[str, Any], company: str) -> Dict[str, Any]:
        """Convert a Greenhouse job record to the canonical shape.

        Greenhouse fields we use:
            - id           → platform-internal numeric id
            - title        → job title
            - updated_at   → ISO timestamp
            - absolute_url → canonical posting URL (the dedup key)
            - location     → ``{"name": "Berlin, DE"}`` object (NEVER ``location_text``;
                             that key does not exist on the public API)
            - content      → HTML body (with ``?content=true``)
            - metadata[]   → array of {name, value} pairs (rarely populated on
                             the public board; salary needs the privileged API)

        Location handling: the public API always sends ``location`` as an
        object, never a plain string. We pull ``location["name"]`` and fall
        back to ``location_text`` for older boards that still send the legacy
        field. Empty string is the documented "Remote" / unstated case.
        """
        url = (raw_job.get("absolute_url") or "").strip()
        # absolute_url can be the boards.greenhouse.io form OR the company's
        # own redirect (e.g. https://stripe.com/jobs/search?gh_jid=8172510).
        # We keep whatever the platform sent — that's what the apply-side
        # adapter will dereference, and it survives slug migration.
        loc = raw_job.get("location")
        if isinstance(loc, dict):
            location = (loc.get("name") or "").strip()
        elif isinstance(loc, str):
            location = loc.strip()
        else:
            location = (raw_job.get("location_text") or "").strip()
        description = raw_job.get("content") or ""

        salary_text: str | None = None
        for meta in raw_job.get("metadata") or []:
            if not isinstance(meta, dict):
                continue
            name = (meta.get("name") or "").lower()
            if name in ("salary", "compensation", "pay"):
                val = meta.get("value")
                if val:
                    salary_text = str(val).strip()
                    break

        return {
            "title": (raw_job.get("title") or "").strip(),
            "company": company,
            "location": location,
            "url": url,
            "source": self.name,
            "description": description,
            "salary_text": salary_text,
            "posted_at": raw_job.get("updated_at"),
        }


__all__ = ["GreenhouseSource"]
