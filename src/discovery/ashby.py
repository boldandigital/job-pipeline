"""Ashby public job board API.

Docs: https://developers.ashbyhq.com/#public-job-board-api
Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}?includeCompensation=false

The ``jobBoardName`` is a per-company slug (e.g. ``openai``, ``anthropic``,
``cohere``). The endpoint returns ``{"jobs": [...], "compensation": [...]}``,
but with ``includeCompensation=false`` the per-job compensation blocks are
omitted (we'd need an API key for those).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import httpx

from .base import BaseDiscoverySource


ASHBY_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbySource(BaseDiscoverySource):
    name = "ashby"

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
                url = f"{ASHBY_BASE}/{slug}?includeCompensation=false"
                payload = self._get_with_retry(client, url)
                if payload is None:
                    time.sleep(self.REQUEST_INTERVAL_SEC)
                    continue
                for job in payload.get("jobs", []) or []:
                    if not isinstance(job, dict):
                        continue
                    # Unlisted postings are draft/internal — skip them so we
                    # don't fill the DB with roles that aren't really public.
                    if job.get("isListed") is False:
                        continue
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
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(self.REQUEST_INTERVAL_SEC * attempt)
                continue
            return None
        return None

    def parse(self, raw_job: Dict[str, Any], company: str) -> Dict[str, Any]:
        """Map an Ashby job record to the canonical job dict.

        Ashby fields we use:
            - id              → UUID
            - title           → job title
            - location        → free text (Ashby usually emits "Remote, EU" or city)
            - department      → e.g. "Engineering"
            - employmentType  → e.g. "FullTime"
            - description     → HTML body
            - applyUrl        → apply link
            - publishedAt     → ISO timestamp
            - isListed        → already filtered upstream
        """
        url = (raw_job.get("applyUrl") or "").strip()
        location = (raw_job.get("location") or "").strip()
        dept = (raw_job.get("department") or "").strip()
        emp = (raw_job.get("employmentType") or "").strip()
        if dept or emp:
            # Many Ashby boards leave the location field empty and only fill
            # department / employmentType — surface both so the user sees them.
            extras = [p for p in (dept, emp) if p]
            if location:
                location = f"{location} ({', '.join(extras)})"
            else:
                location = ", ".join(extras)

        return {
            "title": (raw_job.get("title") or "").strip(),
            "company": company,
            "location": location,
            "url": url,
            "source": self.name,
            "description": raw_job.get("description") or "",
            "salary_text": None,  # requires an API key
            "posted_at": raw_job.get("publishedAt"),
        }


__all__ = ["AshbySource"]
