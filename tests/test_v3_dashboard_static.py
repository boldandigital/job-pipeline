"""
Tests for the v3 4-gate dashboard frontend (web/static/index.html + index.css).

Two layers:

1. Static contract tests — parse the shipped HTML/CSS and assert the wiring the
   card demands: data-job-id, data-gate-stage, the three endpoints, the raw
   score, the top-6 filter and the optimistic rollback logic. These run in
   milliseconds and are what catch a half-finished rewrite (the previous
   attempt shipped a template whose CSS was missing 28 classes).

2. Browser tests (tests/test_v3_dashboard_ui.py) — the real click-through.

Run the browser layer with a live server:
    python -m uvicorn src.web.app:app --port 8742
    V3_UI_BASE_URL=http://127.0.0.1:8742 pytest tests/test_v3_dashboard_ui.py
Without that env var those tests skip.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "web" / "static" / "index.html"
INDEX_CSS = ROOT / "web" / "static" / "index.css"
TEMPLATE_PY = ROOT / "src" / "web" / "template.py"


@pytest.fixture(scope="module")
def html():
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css():
    return INDEX_CSS.read_text(encoding="utf-8")


# ----- template / wiring ---------------------------------------------------

def test_index_html_exists_and_is_v3(html):
    assert INDEX_HTML.exists()
    assert 'data-version="3-4gate"' in html, "root element must carry data-version=3-4gate"


def test_sidebar_and_two_col_grid_shell_exist(html):
    """OD v3 language: sidebar rail + a dedicated grid container."""
    assert 'class="sidebar"' in html
    assert 'id="jobs-grid"' in html


def test_each_gate_carries_its_stage_attribute(html):
    """Every gate button resolves its stage from data-gate-stage on the gate."""
    for stage in ("cv", "anschreiben", "motivation"):
        assert f'data-stage="{stage}"' in html, f"gate stage {stage} missing"


def test_dashboard_renders_raw_scores(html):
    """The circle must show the raw pipeline score (1940), not a 0-100 pct.

    The ring arc may still normalise — but score__num carries the raw number.
    """
    assert "scoreNum.textContent = job.score" in html, \
        "score__num must render the raw job.score"
    # The old v2 normalised by /24 into the number — that must be gone.
    assert "score-num" not in html, "v2 .score-num normalisation must not come back"


def test_gate_limit_and_hidden_statuses_are_configurable(html):
    assert "DASHBOARD_LIMIT = 6" in html, "card brief: top 6 by score"
    assert "HIDDEN_STATUSES" in html, "pipeline output must be filterable"


def test_endpoints_are_wired(html):
    assert "/gate`" in html or "/gate'" in html, "gate endpoint missing"
    assert "/approve" in html
    assert "/skip" in html


def test_gate_optimistic_update_with_rollback_on_failure(html):
    """Optimistic flip, then reconcile; on error the card returns to its
    pre-click state rather than lying to the Captain."""
    assert "optimistic" in html, "gate click must apply state before the fetch resolves"
    assert "applyCardState(card, {" in html
    # A catch branch that restores the captured `before` snapshot.
    assert re.search(r"catch\s*\(e\)\s*\{[^}]*before\.cv_ok", html, re.S), \
        "gate failure path must restore the pre-click state"


def test_approve_requires_all_three_gates(html):
    assert "cv_ok && job.anschreiben_ok && job.motivation_ok" in html


def test_no_auto_submit_anywhere(html):
    """Nothing may submit on load — the final click is always the Captain's."""
    assert "addEventListener('click'" in html
    auto = re.findall(r"\.(?:submit|approve)\(\)", html)
    assert not auto, f"found an implicit submit call: {auto}"


# ----- CSS contract --------------------------------------------------------

def _classes_used(html_text):
    body = re.sub(r"<style.*?</style>|<script.*?</script>", "", html_text, flags=re.S)
    used = set()
    for m in re.finditer(r'class="([^"]+)"', body):
        used.update(m.group(1).split())
    return used


def test_every_rendered_class_has_a_css_rule(html, css):
    """The v3 rewrite shipped once with 28 unstyled classes (silent breakage).

    This is the regression guard: anything the template renders must have a
    selector in the stylesheet.
    """
    used = _classes_used(html)
    missing = sorted(c for c in used if f".{c}" not in css)
    assert not missing, f"rendered but unstyled: {missing}"


def test_grid_is_two_columns_at_desktop_width(css):
    assert re.search(r'\.cards\s*\{[^}]*grid-template-columns:\s*repeat\(2', css), \
        "the card grid must be 2 columns on desktop"


def test_score_ring_is_styled(css):
    for sel in (".score__ring", ".score__bg", ".score__fg", ".score__num"):
        assert sel in css, f"{sel} has no rule — the circle would render bare"


def test_gate_states_are_all_styled(css):
    for state in ("active", "locked", "done"):
        assert f'.gate[data-state="{state}"]' in css, f"gate state {state} unstyled"


def test_approve_enabled_state_is_cyan(css):
    """Acceptance: final Approve becomes cyan once all 3 gates are ok.

    The rule paints with the --accent token rather than a hex literal, so
    resolve the token before asserting.
    """
    block = re.search(
        r'\.approve\[data-state="enabled"\][^{]*\.approve__btn\s*\{([^}]*)\}', css)
    assert block, "enabled approve button has no rule"
    body = block.group(1)
    token = re.search(r"var\(--([\w-]+)\)", body)
    assert token, "enabled approve must paint from a design token"
    token_def = re.search(rf"--{token.group(1)}:\s*(#[0-9A-Fa-f]{{6}})", css)
    assert token_def, f"--{token.group(1)} is not a hex literal in :root"
    assert token_def.group(1).lower() == "#06b6d4", \
        f"--{token.group(1)} must be the cyan brand accent, got {token_def.group(1)}"


def test_skipped_card_greys_out(css):
    block = re.search(r'\.card\[data-state="skipped"\]\s*\{([^}]*)\}', css)
    assert block and "opacity" in block.group(1), "skipped cards must grey out"


# ----- no double render ----------------------------------------------------

def test_server_does_not_inject_a_second_renderer():
    """A stale v2 bootstrap raced the v3 script for #jobs-grid and clobbered
    whichever render landed last. index_page() must serve the file as-is."""
    src = TEMPLATE_PY.read_text(encoding="utf-8")
    # Strip the module docstring — it *describes* the removed bootstrap.
    body = src.split('"""', 2)[-1]
    assert "BOOTSTRAP_JS" not in body, "legacy v2 bootstrap must stay deleted"
    assert "job-card" not in body, "v2 card markup must not be injected"
    assert "return HTMLResponse(INDEX_HTML)" in src