"""Phase 1.6 — deploy-readiness tests.

These tests don't run the heavy deploy machinery; they verify that the
artifacts on disk are wired up correctly. Heavy checks (docker build,
live deploy) live in scripts/check-deploy-readiness.sh.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parent.parent
VERCEL_BACKEND = ROOT / "vercel-backend"


# ─────────────────────────────────────────────────────────────────────────
# vercel.json
# ─────────────────────────────────────────────────────────────────────────

def test_vercel_json_is_valid():
    p = ROOT / "vercel.json"
    assert p.exists(), f"vercel.json missing at {p}"
    data = json.loads(p.read_text())
    assert data["version"] == 2
    assert any(b.get("use") == "@vercel/static" for b in data.get("builds", [])), \
        "vercel.json must include an @vercel/static build for web/static/**"
    routes = data.get("routes", [])
    assert any(r.get("src") == "/" and r.get("dest", "").endswith("marketing.html")
               for r in routes), \
        "vercel.json must route / to marketing.html"
    headers = data.get("headers", [])
    cache_hit = any(
        "Cache-Control" in (h.get("key", "") for h in src.get("headers", []))
        for src in headers
    )
    assert cache_hit, "vercel.json must set Cache-Control on /"


# ─────────────────────────────────────────────────────────────────────────
# vercel-backend/api/index.py
# ─────────────────────────────────────────────────────────────────────────

def test_vercel_backend_index_imports_app():
    """The shim re-exports the FastAPI app so Vercel can serve it."""
    p = VERCEL_BACKEND / "api" / "index.py"
    assert p.exists(), f"vercel-backend/api/index.py missing at {p}"

    sys.path.insert(0, str(VERCEL_BACKEND / "api"))
    try:
        spec = importlib.util.spec_from_file_location("captain_vercel_index", p)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)

    assert hasattr(mod, "app"), "vercel-backend/api/index.py must export `app`"
    assert isinstance(mod.app, FastAPI), \
        f"vercel-backend/api/index.py `app` must be a FastAPI instance, got {type(mod.app).__name__}"
    # Vercel also accepts `handler` as an alias; we set both.
    assert hasattr(mod, "handler"), "vercel-backend/api/index.py should also export `handler`"
    assert mod.handler is mod.app


# ─────────────────────────────────────────────────────────────────────────
# Static pages
# ─────────────────────────────────────────────────────────────────────────

PAGES = [
    ("signup.html", "/api/v1/auth/signup", "/verify"),
    ("verify.html", "/api/v1/auth/verify", "/app/"),
    ("login.html", "/api/v1/auth/login", "/app/"),
]


@pytest.mark.parametrize("filename,endpoint,redirect_target", PAGES)
def test_static_auth_page_exists_and_references_endpoint(filename, endpoint, redirect_target):
    p = ROOT / "web" / "static" / filename
    assert p.exists(), f"{filename} missing"
    html = p.read_text()
    assert endpoint in html, f"{filename} must POST to {endpoint}"
    assert redirect_target in html, f"{filename} must redirect to {redirect_target}"


def test_signup_stashes_email_for_verify_resend():
    """The resend button on verify.html needs the email; signup.html
    must put it in sessionStorage before redirecting."""
    signup = (ROOT / "web" / "static" / "signup.html").read_text()
    assert "sessionStorage" in signup, \
        "signup.html must stash the email in sessionStorage so verify.html can resend"
    assert "captain_signup_email" in signup, \
        "the sessionStorage key must be captain_signup_email (consumed by verify.html)"

    verify = (ROOT / "web" / "static" / "verify.html").read_text()
    assert "captain_signup_email" in verify, \
        "verify.html must read the stashed email to resend"


def test_verify_page_handles_missing_user_id():
    """If the page is opened without ?user_id=..., bounce to signup."""
    verify = (ROOT / "web" / "static" / "verify.html").read_text()
    assert "user_id" in verify
    assert "/signup" in verify, "verify.html must redirect to /signup when user_id is missing"


def test_login_page_handles_forgot_password():
    login = (ROOT / "web" / "static" / "login.html").read_text()
    assert "/api/v1/auth/forgot" in login, "login.html must wire the Forgot button to /api/v1/auth/forgot"


# ─────────────────────────────────────────────────────────────────────────
# Docker artifacts
# ─────────────────────────────────────────────────────────────────────────

def test_vercel_backend_dockerfile_exists():
    p = VERCEL_BACKEND / "Dockerfile"
    assert p.exists()
    txt = p.read_text()
    assert "FROM python" in txt
    assert "vercel-backend/requirements.txt" in txt or "requirements.txt" in txt
    assert "EXPOSE 8742" in txt
    # Stripe env vars should be present
    assert "STRIPE_SECRET_KEY" in txt
    assert "STRIPE_WEBHOOK_SECRET" in txt


def test_vercel_backend_compose_exists():
    p = VERCEL_BACKEND / "docker-compose.yml"
    assert p.exists()
    txt = p.read_text()
    assert "services:" in txt
    # Should reference Stripe env vars
    assert "STRIPE_SECRET_KEY" in txt
    assert "STRIPE_WEBHOOK_SECRET" in txt
    # Should NOT name the service "web" (that's the selfhost one)
    assert "backend" in txt, "compose should use `backend` service name to avoid colliding with selfhost"


def test_vercel_backend_requirements_minimal():
    p = VERCEL_BACKEND / "requirements.txt"
    assert p.exists()
    # Only consider uncommented package lines (no leading '#') when
    # asserting which packages are required. Comments and prose are fine.
    pkg_lines = [
        ln.strip().lower()
        for ln in p.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    joined = "\n".join(pkg_lines)
    # Required runtime deps
    assert "fastapi" in joined
    assert "uvicorn" in joined
    # Forbidden: dev tooling / pipeline stuff (must be a package directive)
    forbidden = ["pytest", "httpx", "patchright", "playwright", "weasyprint", "gspread"]
    for f in forbidden:
        assert not any(ln.startswith(f) for ln in pkg_lines), \
            f"vercel-backend/requirements.txt must not include {f} (it's dev/scraper)"


# ─────────────────────────────────────────────────────────────────────────
# Documentation
# ─────────────────────────────────────────────────────────────────────────

def test_readme_deploy_exists():
    p = ROOT / "README.deploy.md"
    assert p.exists()
    txt = p.read_text()
    # Required sections
    assert "Vercel" in txt
    assert "Fly" in txt or "Render" in txt
    assert "GDPR" in txt or "data residency" in txt.lower()
    assert "TLS" in txt or "Caddy" in txt or "nginx" in txt


def test_env_example_documents_prod_vars():
    txt = (ROOT / ".env.example").read_text()
    required = [
        "CAPTAIN_SESSION_SECRET",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID",
        "DATABASE_URL",
        "SMTP_URL",
    ]
    for v in required:
        assert v in txt, f".env.example must document {v}"


def test_check_deploy_readiness_script_exists_and_executable():
    p = ROOT / "scripts" / "check-deploy-readiness.sh"
    assert p.exists()
    import os
    import stat
    mode = p.stat().st_mode
    assert mode & stat.S_IXUSR, "check-deploy-readiness.sh must be executable"
    txt = p.read_text()
    # Must cover the named acceptance criteria
    assert "marketing.html" in txt
    assert "vercel.json" in txt
    assert "src.web.app" in txt or "from index" in txt
    assert "docker build" in txt
    assert "pytest" in txt
    assert ".env.example" in txt
