"""
HTML wrapper that loads the v2 design and injects real job data
from /api/jobs on page load.
"""
from fastapi.responses import HTMLResponse
from pathlib import Path

INDEX_HTML = (Path(__file__).resolve().parents[2] / "web" / "static" / "index.html").read_text(encoding="utf-8")

# JS bootstrap: fetch /api/jobs, render cards into the grid.
import os as _os
import base64 as _b64
# Legacy: HTTP Basic encoded as a token, used as fallback for iframe-only auth.
# Phase 1.1: session cookies are sent automatically; the token is only used
# to embed in <iframe src="?token=..."> URLs (Phase 1.x backward compat).
_ADMIN_USER = _os.getenv("LARS_USER", "lars")
_ADMIN_PASS = _os.getenv("LARS_PASS", "captain")
_AUTH_TOKEN = _b64.b64encode(f"{_ADMIN_USER}:{_ADMIN_PASS}".encode()).decode().rstrip("=")

BOOTSTRAP_JS = r"""
<script>
(async function() {
  // Auth: use session cookie (Phase 1.1). Falls back to HTTP Basic via
  // /api/v1/auth/login on first 401.
  let AUTH_TOKEN = "__AUTH_TOKEN__";
  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: 'include', ...opts });
    if (r.status === 401) {
      // Fall back: try HTTP Basic via stored token (legacy compat).
      return fetch(path, {
        ...opts,
        headers: { ...opts.headers, 'Authorization': 'Basic ' + atob(AUTH_TOKEN) }
      });
    }
    return r;
  }

  function badgeColor(score) {
    if (score >= 1800) return 'green';
    if (score >= 1000) return 'cyan';
    if (score >= 400) return 'amber';
    return 'slate';
  }

  function buildCardHTML(job) {
    const id = job.id;
    const score = job.score;
    const norm = Math.min(100, Math.round(score / 24));
    const tags = [`XING`, `12y fit`];
    const teaser = (job.description || '').slice(0, 180);
    const url = job.career_url || job.url || '#';
    return `
      <article class="job-card" data-job-id="${id}" data-score="${norm}" data-raw-score="${score}" data-source="${job.source}" data-url="${url}" tabindex="0" aria-expanded="false">
        <header class="job-card__head">
          <div class="score-circle score-circle--${badgeColor(score)}">
            <span class="score-num">${norm}</span>
          </div>
          <div class="job-card__title-block">
            <h3 class="job-card__title">${job.title}</h3>
            <p class="job-card__company"><span>${job.company}</span> <span class="sep">·</span> <span class="loc">${job.location}</span></p>
          </div>
        </header>
        <div class="job-card__meta">
          <span class="job-card__salary">${job.salary}</span>
          ${tags.map(t => `<span class="tag tag--src">${t}</span>`).join('')}
        </div>
        <p class="job-card__teaser">${teaser}…</p>
        <div class="job-card__actions">
          <button class="btn btn--ghost" data-act="open" data-target="${id}">Open ↗</button>
          <button class="btn btn--approve" data-act="approve" data-target="${id}">★ Approve</button>
          <button class="btn btn--skip" data-act="skip" data-target="${id}">✗ Skip</button>
        </div>
      </article>`;
  }

  function buildDetailHTML(job) {
    const token = AUTH_TOKEN;
    const cvSrc = job.cv_path ? `/api/batches/${job.cv_path}?token=${token}#toolbar=0` : '';
    const clSrc = job.cover_letter_path ? `/api/batches/${job.cover_letter_path}?token=${token}#toolbar=0` : '';
    const url = job.career_url || job.url || '#';
    return `
      <div class="job-detail" role="region" aria-label="Job details for ${job.company}">
        <div class="job-detail__head">
          <div>
            <h4 class="job-detail__title">${job.title} — ${job.company}</h4>
            <p class="job-detail__sub">${job.location} · ${job.salary} · ${job.source}</p>
          </div>
          <a class="job-detail__open-xing" href="${url}" target="_blank" rel="noopener">↗ Open in XING</a>
        </div>
        <div class="job-detail__body">
          <div class="job-detail__left">
            <h5>Why this fits Lars</h5>
            <ul class="why__list">
              <li class="why__item"><span class="why__icon">1</span><span><b>Founder + agency operator</b> — matches Senior IC + leadership track.</span></li>
              <li class="why__item"><span class="why__icon">2</span><span><b>DE C2 + EN C2</b> confirmed via CV data.</span></li>
              <li class="why__item"><span class="why__icon">3</span><span><b>Score ${job.score}</b> in pipeline (top ${Math.min(99, Math.round((job.score || 0) / 24))}% match).</span></li>
              <li class="why__item"><span class="why__icon">4</span><span><b>Source:</b> ${job.source} — pipeline-grade data.</span></li>
            </ul>
            <h5>Job description</h5>
            <div class="job-description">${(job.description || '').slice(0, 2000) || '(description not yet fetched — re-run daily pipeline)'}</div>
          </div>
          <div class="job-detail__right">
            <div class="pdf-preview">
              <div class="pdf__head"><b>CV preview</b></div>
              ${cvSrc ? `<iframe src="${cvSrc}#toolbar=0" title="CV preview" loading="lazy"></iframe>` : '<div class="pdf__empty">CV not yet rendered — run daily pipeline first.</div>'}
            </div>
            <div class="pdf-preview">
              <div class="pdf__head"><b>Anschreiben preview</b></div>
              ${clSrc ? `<iframe src="${clSrc}#toolbar=0" title="Anschreiben preview" loading="lazy"></iframe>` : '<div class="pdf__empty">Anschreiben not yet rendered.</div>'}
            </div>
          </div>
        </div>
        <div class="job-detail__actions">
          <button class="btn btn--lg btn--skip" data-act="skip" data-target="${job.id}"><span>✗</span> Skip</button>
          <button class="btn btn--lg btn--xing" data-act="open-xing" data-target="${job.id}"><span>↗</span> Open in XING</button>
          <button class="btn btn--lg btn--primary" data-act="approve" data-target="${job.id}"><span>★</span> Approve &amp; queue submit</button>
        </div>
      </div>`;
  }

  function attachCardEvents(card) {
    card.addEventListener('click', e => {
      // If click is inside the expanded detail or on a button, ignore
      if (e.target.closest('.btn')) return;
      if (e.target.closest('.job-detail__bar')) return;
      if (e.target.closest('.job-detail')) {
        // Click is inside the detail — just keep card open, do nothing else
        return;
      }
      e.stopPropagation();
      const wasExpanded = card.classList.contains('is-active');
      // Collapse all
      document.querySelectorAll('.job-card.is-active').forEach(c => {
        c.classList.remove('is-active');
        const d = c.querySelector('.job-detail');
        if (d) d.remove();
      });
      if (!wasExpanded) {
        card.classList.add('is-active');
        const id = card.dataset.jobId;
        const job = window.__CAPTAIN_JOBS__ && window.__CAPTAIN_JOBS__[id];
        if (job) {
          const detail = document.createElement('div');
          detail.className = 'job-detail';
          detail.innerHTML = buildDetailHTML(job);
          card.appendChild(detail);
          // Wire up the buttons inside the new detail
          detail.querySelectorAll('button[data-act]').forEach(btn => {
            btn.addEventListener('click', async ev => {
              ev.stopPropagation();
              await _handleAction(btn.dataset.act, btn.dataset.target, card, btn);
            });
          });
        }
      }
    });
  }

  async function _handleAction(act, id, card, btn) {
    const job = window.__CAPTAIN_JOBS__[id];
    if (act === 'open' || act === 'open-xing') {
      if (job && (job.career_url || job.url)) window.open(job.career_url || job.url, '_blank');
      return;
    }
    if (act === 'approve') {
      btn.textContent = '✓ Approving…';
      btn.disabled = true;
      const r = await api(`/api/jobs/${id}/approve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      card.classList.add('is-approved');
      btn.textContent = '✓ Approved ' + new Date().toLocaleTimeString();
      return;
    }
    if (act === 'skip') {
      btn.textContent = '⏭ Skipping…';
      btn.disabled = true;
      await api(`/api/jobs/${id}/skip`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      card.classList.add('is-skipped');
      return;
    }
  }

  // Load jobs
  let res;
  try { res = await api('/api/jobs?status=new&limit=20'); }
  catch (e) { console.error('fetch /api/jobs failed', e); return; }
  if (!res || !res.ok) {
    document.getElementById('jobs-grid').innerHTML =
      '<div style="padding:32px;color:#fbbf24">⚠ Failed to load jobs. Check FastAPI console.</div>';
    return;
  }
  const jobs = await res.json();
  window.__CAPTAIN_JOBS__ = Object.fromEntries(jobs.map(j => [String(j.id), j]));

  // Render cards
  const grid = document.getElementById('jobs-grid');
  grid.innerHTML = '';
  jobs.forEach(job => {
    const div = document.createElement('div');
    div.innerHTML = buildCardHTML(job);
    const card = div.firstElementChild;
    grid.appendChild(card);
    attachCardEvents(card);
  });

  // Update hero count
  const heroCount = document.querySelector('.hero h1, .hero-headline');
  if (heroCount) heroCount.textContent = jobs.length + ' new roles — your move, Captain.';

  // Load stats
  try {
    const statsRes = await api('/api/stats');
    if (statsRes.ok) {
      const stats = await statsRes.json();
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set('cnt-today', stats.today_new);
      set('cnt-autofit', stats.high_fit);
      set('cnt-new', jobs.length);
    }
  } catch (e) { console.warn('stats fetch failed', e); }
})();
</script>
"""


def index_page() -> HTMLResponse:
    """Serve the v3 4-gate dashboard as-is.

    The v3 dashboard has its own self-contained JS (renders cards from
    /api/jobs, wires gate buttons, etc.) and does NOT need the legacy
    template.py bootstrap. We detect the v3 dashboard by `data-version="3-4gate"`
    on the root element and skip the legacy append.
    """
    if 'data-version="3-4gate"' in INDEX_HTML:
        return HTMLResponse(INDEX_HTML)
    # Legacy v2 fallback
    rendered = BOOTSTRAP_JS.replace("__AUTH_TOKEN__", _AUTH_TOKEN)
    html = INDEX_HTML.replace("</body>", rendered + "\n</body>")
    return HTMLResponse(html)
