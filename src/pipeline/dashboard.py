#!/usr/bin/env python3
"""
Job search dashboard -- web UI for reviewing and acting on scored jobs.

Features:
- View top-scored jobs with filtering
- Apply / Skip / Save for later
- Filter by score, source, status
- Mobile-friendly responsive design

Usage:
    python -m src.pipeline.dashboard
    python -m src.pipeline.dashboard --port 9000 --db ./data/jobs.db
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dashboard")


def get_db(db_path):
    """Get database connection with WAL mode."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_dashboard_html():
    """Return the dashboard HTML/CSS/JS."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Job Search Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }
        .header { background: #2c5282; color: white; padding: 15px 20px; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 1.3em; }
        .stats { display: flex; gap: 15px; font-size: 0.9em; }
        .stat { background: rgba(255,255,255,0.15); padding: 4px 10px; border-radius: 4px; }
        .filters { background: white; padding: 12px 20px; border-bottom: 1px solid #e2e8f0; display: flex; gap: 10px; flex-wrap: wrap; }
        .filters select, .filters input { padding: 6px 10px; border: 1px solid #cbd5e0; border-radius: 4px; font-size: 0.9em; }
        .filters button { padding: 6px 15px; background: #2c5282; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .jobs { padding: 15px 20px; }
        .job-card { background: white; border-radius: 8px; padding: 15px; margin-bottom: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .job-header { display: flex; justify-content: space-between; align-items: flex-start; }
        .job-title { font-size: 1.05em; font-weight: 600; color: #2c5282; }
        .job-title a { color: inherit; text-decoration: none; }
        .job-title a:hover { text-decoration: underline; }
        .job-score { font-size: 1.2em; font-weight: bold; padding: 2px 10px; border-radius: 4px; }
        .score-high { background: #c6f6d5; color: #22543d; }
        .score-mid { background: #fefcbf; color: #744210; }
        .score-low { background: #fed7d7; color: #742a2a; }
        .job-meta { color: #718096; font-size: 0.85em; margin-top: 4px; }
        .job-actions { margin-top: 8px; display: flex; gap: 6px; }
        .btn { padding: 4px 12px; border: 1px solid #cbd5e0; border-radius: 4px; cursor: pointer; font-size: 0.8em; background: white; }
        .btn-apply { border-color: #48bb78; color: #22543d; }
        .btn-skip { border-color: #fc8181; color: #742a2a; }
        .btn:hover { opacity: 0.8; }
        .pagination { text-align: center; padding: 15px; }
        .pagination button { margin: 0 5px; }
        @media (max-width: 600px) { .filters { flex-direction: column; } .job-header { flex-direction: column; } }
    </style>
</head>
<body>
    <div class="header">
        <h1>Job Search Dashboard</h1>
        <div class="stats" id="stats"></div>
    </div>
    <div class="filters">
        <select id="filterSource"><option value="">All Sources</option></select>
        <select id="filterStatus">
            <option value="">All Status</option>
            <option value="new" selected>New</option>
            <option value="batched">Batched</option>
            <option value="applied">Applied</option>
            <option value="skipped">Skipped</option>
        </select>
        <input type="number" id="filterMinScore" placeholder="Min Score" value="20" style="width:100px">
        <input type="text" id="filterSearch" placeholder="Search title/company...">
        <button onclick="loadJobs()">Filter</button>
    </div>
    <div class="jobs" id="jobsList"></div>
    <div class="pagination" id="pagination"></div>

    <script>
    let currentPage = 1;
    const perPage = 25;

    async function loadJobs() {
        const params = new URLSearchParams({
            source: document.getElementById('filterSource').value,
            status: document.getElementById('filterStatus').value,
            min_score: document.getElementById('filterMinScore').value || '0',
            search: document.getElementById('filterSearch').value,
            page: currentPage,
            per_page: perPage,
        });

        const resp = await fetch('/api/jobs?' + params);
        const data = await resp.json();

        document.getElementById('stats').innerHTML =
            `<span class="stat">Total: ${data.total}</span>` +
            `<span class="stat">Page: ${currentPage}/${Math.ceil(data.total/perPage) || 1}</span>`;

        const container = document.getElementById('jobsList');
        container.innerHTML = data.jobs.map(job => {
            const scoreClass = job.score >= 150 ? 'score-high' : job.score >= 80 ? 'score-mid' : 'score-low';
            const url = job.career_url || job.url || '#';
            return `<div class="job-card">
                <div class="job-header">
                    <div>
                        <div class="job-title"><a href="${url}" target="_blank">${escHtml(job.title)}</a></div>
                        <div class="job-meta">${escHtml(job.company)} &middot; ${escHtml(job.location || '')} &middot; ${job.source} &middot; ${job.status}</div>
                    </div>
                    <div class="job-score ${scoreClass}">${job.score}</div>
                </div>
                <div class="job-actions">
                    <button class="btn btn-apply" onclick="updateStatus(${job.id}, 'applied')">Applied</button>
                    <button class="btn btn-skip" onclick="updateStatus(${job.id}, 'skipped')">Skip</button>
                    <button class="btn" onclick="updateStatus(${job.id}, 'saved')">Save</button>
                </div>
            </div>`;
        }).join('');
    }

    function escHtml(s) {
        return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    async function updateStatus(id, status) {
        await fetch('/api/jobs/' + id, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status})
        });
        loadJobs();
    }

    // Load sources for filter
    fetch('/api/sources').then(r => r.json()).then(sources => {
        const sel = document.getElementById('filterSource');
        sources.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; sel.appendChild(o); });
    });

    loadJobs();
    </script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard."""

    db_path = DB_PATH

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(get_dashboard_html().encode())

        elif parsed.path == "/api/jobs":
            params = parse_qs(parsed.query)
            source = params.get("source", [""])[0]
            status = params.get("status", [""])[0]
            min_score = int(params.get("min_score", ["0"])[0])
            search = params.get("search", [""])[0]
            page = int(params.get("page", ["1"])[0])
            per_page = int(params.get("per_page", ["25"])[0])

            conn = get_db(self.db_path)
            conditions = ["score >= ?"]
            values = [min_score]

            if source:
                conditions.append("source = ?")
                values.append(source)
            if status:
                conditions.append("status = ?")
                values.append(status)
            if search:
                conditions.append("(title LIKE ? OR company LIKE ?)")
                values.extend([f"%{search}%", f"%{search}%"])

            where = " AND ".join(conditions)
            total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", values).fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT id, title, company, location, url, career_url, score, source, status FROM jobs WHERE {where} ORDER BY score DESC LIMIT ? OFFSET ?",
                values + [per_page, offset],
            ).fetchall()

            jobs = [dict(r) for r in rows]
            conn.close()

            self.send_json({"jobs": jobs, "total": total, "page": page})

        elif parsed.path == "/api/sources":
            conn = get_db(self.db_path)
            sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM jobs WHERE source IS NOT NULL ORDER BY source").fetchall()]
            conn.close()
            self.send_json(sources)

        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        if self.path.startswith("/api/jobs/"):
            job_id = int(self.path.split("/")[-1])
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            conn = get_db(self.db_path)
            conn.execute("UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (body.get("status", "new"), job_id))
            conn.commit()
            conn.close()

            self.send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)


def main():
    parser = argparse.ArgumentParser(description="Job search dashboard")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    DashboardHandler.db_path = args.db

    server = HTTPServer((args.host, args.port), DashboardHandler)
    log.info("Dashboard running on http://%s:%d", args.host, args.port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
