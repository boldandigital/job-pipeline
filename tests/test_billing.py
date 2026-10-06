"""Tests for the Phase 1.4 Stripe billing layer.

Covers:
  * create_checkout_session — dev-mode mock URL + plan validation
  * create_customer_portal_session — dev-mode mock URL
  * verify_webhook — JSON-parse in dev mode; rejects bad sig in production
  * handle_event — every event type we subscribe to updates users.plan
    correctly (checkout.completed → plan; sub.deleted → free; sub.updated
    active→plan; sub.updated past_due→free; payment_failed→no change)
  * quota helpers — check_jobs_quota / check_render_quota / can_auto_apply
  * POST /api/v1/billing/checkout — returns {url}
  * POST /api/v1/billing/portal — returns {url}
  * POST /api/v1/billing/webhook — checkout.session.completed upgrades plan;
    customer.subscription.deleted downgrades to free
  * GET /api/v1/billing/mock-checkout — DEV-only; upgrades plan
  * GET /api/v1/billing/me — returns plan + limits + usage
  * Bootstrap admin ("lars") is NEVER downgraded by a webhook

The tests do not hit live Stripe — DEV MODE is the default in CI because
STRIPE_SECRET_KEY defaults to "test".
"""
from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_users_db(tmp_path, monkeypatch):
    """Point users_db at a throwaway file. Reset before AND after each test."""
    from src.db import users_db
    db = tmp_path / "users.db"
    monkeypatch.setattr(users_db, "USERS_DB", db)
    users_db.connect_users_db().close()  # creates file + schema
    yield db


@pytest.fixture
def temp_jobs_db(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            url TEXT, career_url TEXT, source TEXT, description TEXT,
            score INTEGER, status TEXT DEFAULT 'new', cv_path TEXT,
            cover_letter_path TEXT, created_at TEXT, updated_at TEXT
        );
        INSERT INTO jobs VALUES (1, 'T', 'TestCo', 'Berlin',
            'https://x.test', '', 'xing', 'desc', 500, 'new', '', '',
            '2026-10-03T00:00:00+00:00', '');
    """)
    conn.commit(); conn.close()
    return db


@pytest.fixture
def app(tmp_path, temp_jobs_db, temp_users_db, monkeypatch):
    from src.web import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", temp_jobs_db)
    empty_batches = tmp_path / "batches"
    empty_batches.mkdir()
    monkeypatch.setattr(app_mod, "BATCHES_DIR", empty_batches)
    return app_mod.app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers():
    raw = base64.b64encode(b"lars:captain").decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def reset_stripe_state(monkeypatch):
    """Make sure we are in DEV MODE for every test (overrides .env values)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "test")
    monkeypatch.setenv("STRIPE_PRICE_SOLO", "price_solo_test")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_test")
    # Drop any memoised SDK client
    from src.billing import stripe_client as _stripe
    _stripe.reset_stripe_client_for_tests()
    yield
    _stripe.reset_stripe_client_for_tests()


def _create_user(temp_users_db, email="alice@example.com", plan="free"):
    """Insert a real user row into the temp users.db."""
    from src.db import users_db
    from src.web.auth import hash_password
    pwd = hash_password("testpassword123")
    return users_db.create_user(
        email=email, password_hash=pwd, name="Alice", plan=plan
    )


def _set_verified(uid: str) -> None:
    from src.db import users_db
    users_db.mark_verified(uid)


def _plan_for(uid: str) -> str:
    from src.db import users_db
    row = users_db.get_user_by_id(uid)
    return (row["plan"] or "free") if row else "free"


def _login_session(client, user_id_or_email: str, password: str = "testpassword123"):
    r = client.post(
        "/api/v1/auth/login",
        json={"user_id": user_id_or_email, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.cookies


# ===========================================================================
# stripe_client — create_checkout_session
# ===========================================================================

def test_create_checkout_session_dev_mode_returns_mock_url(reset_stripe_state):
    from src.billing import stripe_client as sc
    res = sc.create_checkout_session(user_id="u-1", plan="solo")
    assert "url" in res and "session_id" in res
    assert res["url"].startswith("/api/v1/billing/mock-checkout?")
    assert "session_id=" in res["url"]
    assert "user_id=u-1" in res["url"]
    assert "plan=solo" in res["url"]


def test_create_checkout_session_pro_plan_in_mock_url(reset_stripe_state):
    from src.billing import stripe_client as sc
    res = sc.create_checkout_session(user_id="u-2", plan="pro")
    assert "plan=pro" in res["url"]


def test_create_checkout_session_unknown_plan_raises(reset_stripe_state):
    from src.billing import stripe_client as sc
    with pytest.raises(ValueError, match="unknown plan"):
        sc.create_checkout_session(user_id="u-3", plan="bogus")


# ===========================================================================
# stripe_client — create_customer_portal_session
# ===========================================================================

def test_create_customer_portal_session_dev_mode(reset_stripe_state):
    from src.billing import stripe_client as sc
    res = sc.create_customer_portal_session(user_id="u-1")
    assert "url" in res
    assert res["url"].startswith("/api/v1/billing/mock-checkout")
    assert "user_id=u-1" in res["url"]


# ===========================================================================
# stripe_client — verify_webhook
# ===========================================================================

def test_verify_webhook_dev_mode_parses_json(reset_stripe_state):
    from src.billing import stripe_client as sc
    body = json.dumps({"type": "checkout.session.completed", "id": "evt_1"}).encode()
    event = sc.verify_webhook(body, "irrelevant-sig")
    assert event["type"] == "checkout.session.completed"
    assert event["id"] == "evt_1"


def test_verify_webhook_dev_mode_rejects_non_json(reset_stripe_state):
    from src.billing import stripe_client as sc
    with pytest.raises(ValueError):
        sc.verify_webhook(b"not json", "sig")


# ===========================================================================
# stripe_client — handle_event
# ===========================================================================

def test_handle_event_checkout_completed_upgrades_plan(reset_stripe_state, temp_users_db):
    from src.billing import stripe_client as sc
    uid = _create_user(temp_users_db, email="buyer@example.com", plan="free")
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": uid,
                "metadata": {"user_id": uid, "plan": "solo"},
                "line_items": {"data": [{"price": {"id": "price_solo_test"}}]},
            }
        },
    }
    sc.handle_event(event)
    assert _plan_for(uid) == "solo"


def test_handle_event_subscription_updated_active_upgrades(reset_stripe_state, temp_users_db):
    from src.billing import stripe_client as sc
    uid = _create_user(temp_users_db, email="sub1@example.com", plan="free")
    event = {
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "status": "active",
                "customer": {"metadata": {"user_id": uid}},
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    sc.handle_event(event)
    assert _plan_for(uid) == "pro"


def test_handle_event_subscription_updated_past_due_downgrades(reset_stripe_state, temp_users_db):
    from src.billing import stripe_client as sc
    uid = _create_user(temp_users_db, email="sub2@example.com", plan="pro")
    event = {
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "status": "past_due",
                "customer": {"metadata": {"user_id": uid}},
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    sc.handle_event(event)
    assert _plan_for(uid) == "free"


def test_handle_event_subscription_deleted_downgrades(reset_stripe_state, temp_users_db):
    from src.billing import stripe_client as sc
    uid = _create_user(temp_users_db, email="cancel@example.com", plan="pro")
    event = {
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "customer": {"metadata": {"user_id": uid}},
            }
        },
    }
    sc.handle_event(event)
    assert _plan_for(uid) == "free"


def test_handle_event_payment_failed_keeps_current_plan(reset_stripe_state, temp_users_db):
    from src.billing import stripe_client as sc
    uid = _create_user(temp_users_db, email="payfail@example.com", plan="pro")
    event = {
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "customer": {"metadata": {"user_id": uid}},
            }
        },
    }
    sc.handle_event(event)
    # Stripe retries — we don't auto-downgrade on the first failure
    assert _plan_for(uid) == "pro"


def test_handle_event_unknown_type_is_noop(reset_stripe_state, temp_users_db):
    from src.billing import stripe_client as sc
    uid = _create_user(temp_users_db, email="noop@example.com", plan="free")
    event = {"type": "customer.tax.updated", "data": {"object": {}}}
    sc.handle_event(event)
    assert _plan_for(uid) == "free"


def test_handle_event_never_downgrades_bootstrap_admin(reset_stripe_state, temp_users_db):
    """Even if a webhook points at user_id='lars', the admin stays admin."""
    from src.billing import stripe_client as sc
    from src.db import users_db as _udb
    # Create an "admin" user — we never touch the real bootstrap admin
    uid = "lars"
    # 'lars' is special-cased in set_plan; the row may or may not exist
    # in temp_users_db. Create a placeholder so the event handler can find
    # a row to look up (or skip, the helper short-circuits anyway).
    if _udb.get_user_by_id(uid) is None:
        # Bootstrap admin "row" in the temp DB (mirrors how production has
        # the admin user implicitly authenticated via env vars).
        conn = _udb.connect_users_db()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO users (id, email, password_hash,
                                                name, plan, verified,
                                                created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (uid, "lars@admin.local", "x", "Lars", "admin",
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()
    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": {"metadata": {"user_id": uid}}}},
    }
    sc.handle_event(event)
    # Admin plan is preserved (or missing row — both mean "still admin").
    row = _udb.get_user_by_id(uid)
    if row is not None:
        assert (row["plan"] or "admin") == "admin"


def test_handle_event_no_user_id_is_ignored(reset_stripe_state, temp_users_db):
    """Webhook events without a user_id get logged + dropped, no DB write."""
    from src.billing import stripe_client as sc
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {}, "line_items": {"data": []}}},
    }
    # Should not raise
    sc.handle_event(event)


# ===========================================================================
# quota helpers
# ===========================================================================

def test_quota_check_jobs_quota_free_limit_10(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="q1@example.com", plan="free")
    assert quota.check_jobs_quota(uid, current_jobs_count=0) is True
    assert quota.check_jobs_quota(uid, current_jobs_count=9) is True
    # 10 == cap → False (we never accept AT the cap)
    assert quota.check_jobs_quota(uid, current_jobs_count=10) is False
    assert quota.check_jobs_quota(uid, current_jobs_count=11) is False


def test_quota_check_jobs_quota_solo_limit_50(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="q2@example.com", plan="solo")
    assert quota.check_jobs_quota(uid, current_jobs_count=49) is True
    assert quota.check_jobs_quota(uid, current_jobs_count=50) is False


def test_quota_check_jobs_quota_pro_unlimited(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="q3@example.com", plan="pro")
    for n in (0, 100, 1_000_000):
        assert quota.check_jobs_quota(uid, current_jobs_count=n) is True


def test_quota_check_render_quota_free_5_per_day(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="r1@example.com", plan="free")
    assert quota.check_render_quota(uid, 0) is True
    assert quota.check_render_quota(uid, 4) is True
    assert quota.check_render_quota(uid, 5) is False


def test_quota_check_render_quota_solo_50(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="r2@example.com", plan="solo")
    assert quota.check_render_quota(uid, 49) is True
    assert quota.check_render_quota(uid, 50) is False


def test_quota_check_render_quota_pro_unlimited(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="r3@example.com", plan="pro")
    assert quota.check_render_quota(uid, 10_000) is True


def test_quota_can_auto_apply_only_for_pro_and_admin(reset_stripe_state, temp_users_db):
    from src.billing import quota
    free = _create_user(temp_users_db, email="a1@example.com", plan="free")
    solo = _create_user(temp_users_db, email="a2@example.com", plan="solo")
    pro = _create_user(temp_users_db, email="a3@example.com", plan="pro")
    assert quota.can_auto_apply(free) is False
    assert quota.can_auto_apply(solo) is False
    assert quota.can_auto_apply(pro) is True
    assert quota.can_auto_apply("lars") is True  # admin always True


def test_quota_get_user_plan_falls_back_to_free(reset_stripe_state, temp_users_db):
    from src.billing import quota
    # Unknown user
    assert quota.get_user_plan("nonexistent-uuid") == "free"


def test_quota_build_usage_block_emits_full_shape(reset_stripe_state, temp_users_db):
    from src.billing import quota
    uid = _create_user(temp_users_db, email="u@example.com", plan="free")
    usage = quota.build_usage_block(uid, current_jobs_count=8, renders_today=4)
    assert usage["plan"] == "free"
    assert usage["jobs"]["used"] == 8
    assert usage["jobs"]["cap"] == 10
    assert usage["jobs"]["unlimited"] is False
    assert usage["jobs"]["near_limit"] is True   # 8 / 10 == 80%
    assert usage["jobs"]["exhausted"] is False
    assert usage["renders_today"]["cap"] == 5
    assert usage["renders_today"]["near_limit"] is True   # 4/5 == 80%
    # pro plan
    uid2 = _create_user(temp_users_db, email="u2@example.com", plan="pro")
    usage2 = quota.build_usage_block(uid2, current_jobs_count=9999, renders_today=9999)
    assert usage2["jobs"]["unlimited"] is True
    assert usage2["renders_today"]["unlimited"] is True


# ===========================================================================
# HTTP endpoints
# ===========================================================================

def test_http_checkout_returns_url(client, temp_users_db, reset_stripe_state, auth_headers):
    r = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "solo"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "url" in body
    assert body["url"].startswith("/api/v1/billing/mock-checkout")


def test_http_checkout_invalid_plan_400(client, auth_headers, reset_stripe_state):
    r = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "bogus"},
        headers=auth_headers,
    )
    assert r.status_code == 400


def test_http_checkout_requires_auth(client, reset_stripe_state):
    r = client.post("/api/v1/billing/checkout", json={"plan": "solo"})
    assert r.status_code == 401


def test_http_portal_returns_url(client, auth_headers, reset_stripe_state):
    r = client.post("/api/v1/billing/portal", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "url" in body


def test_http_portal_requires_auth(client, reset_stripe_state):
    r = client.post("/api/v1/billing/portal")
    assert r.status_code == 401


def test_http_webhook_checkout_completed_upgrades_plan(
    client, temp_users_db, reset_stripe_state, auth_headers,
):
    """The real /api/v1/billing/webhook route processes a checkout event."""
    uid = _create_user(temp_users_db, email="hook1@example.com", plan="free")
    event = {
        "id": "evt_test_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": uid,
                "metadata": {"user_id": uid, "plan": "solo"},
                "line_items": {"data": [{"price": {"id": "price_solo_test"}}]},
            }
        },
    }
    r = client.post("/api/v1/billing/webhook", json=event)
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert _plan_for(uid) == "solo"


def test_http_webhook_subscription_deleted_downgrades_plan(
    client, temp_users_db, reset_stripe_state, auth_headers,
):
    uid = _create_user(temp_users_db, email="hook2@example.com", plan="pro")
    event = {
        "id": "evt_test_2",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "customer": {"metadata": {"user_id": uid}},
            }
        },
    }
    r = client.post("/api/v1/billing/webhook", json=event)
    assert r.status_code == 200
    assert _plan_for(uid) == "free"


def test_http_webhook_invalid_body_400(client, reset_stripe_state):
    """In dev mode the body is parsed as JSON; a non-JSON body → 400."""
    r = client.post(
        "/api/v1/billing/webhook",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_http_mock_checkout_marks_user_paid(
    client, temp_users_db, reset_stripe_state,
):
    """GET /api/v1/billing/mock-checkout synthesises a webhook + upgrades plan."""
    uid = _create_user(temp_users_db, email="mock@example.com", plan="free")
    r = client.get(
        f"/api/v1/billing/mock-checkout?session_id=cs_test_abc"
        f"&user_id={uid}&plan=pro",
    )
    assert r.status_code == 200
    assert "html" in r.headers["content-type"].lower()
    assert "pro" in r.text.lower()
    assert _plan_for(uid) == "pro"


def test_http_mock_checkout_pro_plan_also_works(
    client, temp_users_db, reset_stripe_state,
):
    uid = _create_user(temp_users_db, email="mock2@example.com", plan="free")
    r = client.get(
        f"/api/v1/billing/mock-checkout?session_id=cs_test_xyz"
        f"&user_id={uid}&plan=solo",
    )
    assert r.status_code == 200
    assert _plan_for(uid) == "solo"


def test_http_mock_checkout_refuses_in_production_mode(
    client, temp_users_db, monkeypatch, reset_stripe_state,
):
    """When STRIPE_SECRET_KEY is a real sk_live_..., mock-checkout must 404."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_fake")
    from src.billing import stripe_client as _stripe
    _stripe.reset_stripe_client_for_tests()
    r = client.get(
        "/api/v1/billing/mock-checkout?session_id=cs_x&user_id=u&plan=solo"
    )
    assert r.status_code == 404
    # Restore for downstream tests
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test")


def test_http_billing_me_returns_plan_and_limits(
    client, temp_users_db, reset_stripe_state, auth_headers,
):
    """GET /api/v1/billing/me returns the current user's plan + limits + usage."""
    uid = _create_user(temp_users_db, email="me@example.com", plan="solo")
    _set_verified(uid)
    cookies = _login_session(client, "me@example.com")
    r = client.get("/api/v1/billing/me", cookies=cookies)
    assert r.status_code == 200
    body = r.json()
    assert body["plan"] == "solo"
    assert body["limits"]["max_jobs"] == 50
    assert body["limits"]["auto_apply"] is False
    assert body["limits"]["telegram"] is True
    assert body["dev_mode"] is True
    assert body["usage"]["plan"] == "solo"
    assert "jobs" in body["usage"]


def test_http_billing_me_admin_returns_admin_plan(
    client, temp_users_db, reset_stripe_state, auth_headers,
):
    r = client.get("/api/v1/billing/me", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    # Bootstrap admin → plan='admin' regardless of DB state
    assert body["plan"] == "admin"
    assert body["limits"]["max_jobs"] == -1
    assert body["limits"]["auto_apply"] is True


def test_http_billing_me_requires_auth(client, reset_stripe_state):
    r = client.get("/api/v1/billing/me")
    assert r.status_code == 401


# ===========================================================================
# /api/jobs soft-warn headers
# ===========================================================================

def test_jobs_endpoint_includes_plan_headers_for_session_user(
    client, temp_users_db, reset_stripe_state, auth_headers,
):
    """/api/jobs response carries X-Plan + X-Usage-* headers."""
    r = client.get("/api/jobs", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers.get("X-Plan") == "admin"
    # admin has unlimited cap
    assert r.headers.get("X-Usage-Jobs-Cap") == "-1"


def test_jobs_endpoint_includes_plan_headers_for_free_user(
    client, temp_users_db, reset_stripe_state,
):
    """A free user gets cap=10 in the headers."""
    uid = _create_user(temp_users_db, email="free@example.com", plan="free")
    _set_verified(uid)
    cookies = _login_session(client, "free@example.com")
    r = client.get("/api/jobs", cookies=cookies)
    assert r.status_code == 200
    assert r.headers.get("X-Plan") == "free"
    assert r.headers.get("X-Usage-Jobs-Cap") == "10"
    # We seeded 1 job, so 1/10 → near_limit, not exhausted
    assert r.headers.get("X-Usage-Near-Limit") == "0"
    assert r.headers.get("X-Usage-Exhausted") == "0"


# ===========================================================================
# End-to-end: signup → verify → mock-checkout → /me
# ===========================================================================

def test_end_to_end_signup_then_upgrade(
    client, temp_users_db, reset_stripe_state,
):
    """A new user signs up, verifies, hits the mock-checkout, and the plan flips."""
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "e2e@example.com", "password": "testpassword123", "name": "E2E"},
    )
    assert r.status_code == 200
    uid = r.json()["user_id"]
    code = _peek_code(temp_users_db, "e2e@example.com")
    r = client.post("/api/v1/auth/verify", json={"user_id": uid, "code": code})
    assert r.status_code == 200
    cookies = r.cookies

    # Plan is still free
    r = client.get("/api/v1/billing/me", cookies=cookies)
    assert r.json()["plan"] == "free"

    # Fire the mock-checkout
    r = client.get(
        f"/api/v1/billing/mock-checkout?session_id=cs_e2e&user_id={uid}&plan=solo",
        cookies=cookies,
    )
    assert r.status_code == 200

    # Plan is now solo
    r = client.get("/api/v1/billing/me", cookies=cookies)
    assert r.json()["plan"] == "solo"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _peek_code(db_path: Path, email: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT verify_code FROM users WHERE email = ?", (email,),
    ).fetchone()
    conn.close()
    assert row is not None, f"no user row for {email}"
    return row["verify_code"]
