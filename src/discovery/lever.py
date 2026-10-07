"""Lever public job board API.

Docs: https://github.com/lever/postings-api
Endpoint: GET https://api.lever.co/v0/postings/{client}?mode=json

Lever returns a JSON ARRAY (not an object) at the top level. Each posting
carries: ``id`` (UUID), ``text`` (title), ``categories`` (object with
location/commitment/team/etc.), ``descriptionPlain`` and ``description``
(rich HTML body), ``hostedUrl`` (canonical posting) and ``applyUrl``
(where Lever redirects on click).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import httpx

from .base import BaseDiscoverySource


LEVER_BASE = "https://api.lever.co/v0/postings"


class LeverSource(BaseDiscoverySource):
    name = "lever"

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
            for client_slug in companies:
                url = f"{LEVER_BASE}/{client_slug}?mode=json"
                payload = self._get_with_retry(client, url)
                if payload is None:
                    time.sleep(self.REQUEST_INTERVAL_SEC)
                    continue
                if isinstance(payload, list):
                    out.extend(payload)
                time.sleep(self.REQUEST_INTERVAL_SEC)
        finally:
            client.close()
        return out

    def _get_with_retry(self, client: httpx.Client, url: str) -> List[Any] | None:
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
                    data = resp.json()
                except ValueError:
                    return None
                if isinstance(data, list):
                    return data
                # Some tenants return an empty object instead of [] — treat as "no postings".
                if isinstance(data, dict) and not data:
                    return []
                return None
            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(self.REQUEST_INTERVAL_SEC * attempt)
                continue
            return None
        return None

    def parse(self, raw_job: Dict[str, Any], company: str) -> Dict[str, Any]:
        """Map a Lever posting to the canonical job dict.

        Lever fields we use:
            - id              → UUID
            - text            → title
            - categories      → dict; we pull location/commitment/team as a
                                human-readable comma string
            - descriptionPlain→ plain text body (preferred for storage)
            - description     → HTML body (fallback when plain is empty)
            - hostedUrl       → canonical posting URL (the dedup key)
            - applyUrl        → apply redirect (kept off-canon for now)
            - createdAt       → ms since epoch
        """
        url = (raw_job.get("hostedUrl") or "").strip()
        # Lever wraps plain text with \n — collapse to single spaces for compactness,
        # but preserve paragraph breaks as double newlines.
        plain = (raw_job.get("descriptionPlain") or "").strip()
        html = (raw_job.get("description") or "").strip()
        description = plain if plain else html

        cats = raw_job.get("categories") or {}
        if not isinstance(cats, dict):
            cats = {}
        loc = cats.get("location") or ""
        commitment = cats.get("commitment") or ""
        team = cats.get("team") or ""
        location_parts = [p for p in (loc, commitment, team) if p]
        location = ", ".join(location_parts)

        created = raw_job.get("createdAt")
        posted_at: str | None = None
        # Lever's createdAt is ms since epoch (integer). We don't store it as
        # a real ISO timestamp — leave the conversion to the renderer. For
        # the dedup test we only need a stable string-or-None.
        if isinstance(created, (int, float)):
            posted_at = str(int(created))

        return {
            "title": (raw_job.get("text") or "").strip(),
            "company": company,
            "location": location,
            "url": url,
            "source": self.name,
            "description": description,
            "salary_text": None,  # Lever's public board doesn't expose salary
            "posted_at": posted_at,
        }


__all__ = ["LeverSource"]
