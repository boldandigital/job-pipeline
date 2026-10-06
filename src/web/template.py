"""
Serves the Captain's Bridge dashboard.

As of the v3 4-gate rewrite the page ships its own complete <script> block
(web/static/index.html) — it fetches /api/jobs, renders the cards and drives
the gate/approve/skip endpoints. This module is now a thin pass-through.

Do NOT inject a second renderer here. The legacy v2 BOOTSTRAP_JS was deleted
on purpose: it rendered v2 `.job-card` markup into the same #jobs-grid, so
whichever fetch() resolved last won and the page flickered between two
different layouts (and served stale /api/jobs?status=new data).
"""
from pathlib import Path

from fastapi.responses import HTMLResponse

STATIC_DIR = Path(__file__).resolve().parents[2] / "web" / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def index_page() -> HTMLResponse:
    """Serve the dashboard markup as-is — v3 owns its own bootstrap."""
    return HTMLResponse(INDEX_HTML)