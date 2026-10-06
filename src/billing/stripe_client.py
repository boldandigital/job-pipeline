"""Stripe SDK singleton + Checkout / Portal / Webhook helpers.

Design constraints (Phase 1.4):
  * NO live API calls unless STRIPE_SECRET_KEY is a real ``sk_live_…``
    key. Anything else (``""``, ``"test"``, ``"sk_test_…"``) → DEV MODE.
  * The bootstrap admin user ("lars") must keep plan='admin' regardless
    of any incoming webhook event — we never downgrade him.
  * The module-level ``_client`` is built lazily so unit tests can
    monkeypatch env vars before the first call.
  * Webhook signature verification is skipped in DEV MODE (no signing
    secret in use) but always run in production.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

from src.db import users_db
from src.db.jobs_db import ADMIN_USER_ID

log = logging.getLogger("billing.stripe_client")

# ---------------------------------------------------------------------------
# Env-driven config
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "test").strip()
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "test").strip()
STRIPE_PRICE_SOLO: str = os.getenv("STRIPE_PRICE_SOLO", "price_solo_dev").strip()
STRIPE_PRICE_PRO: str = os.getenv("STRIPE_PRICE_PRO", "price_pro_dev").strip()
STRIPE_SUCCESS_URL: str = os.getenv(
    "STRIPE_SUCCESS_URL", "http://localhost:8000/app/?upgraded=1"
).strip()
STRIPE_CANCEL_URL: str = os.getenv(
    "STRIPE_CANCEL_URL", "http://localhost:8000/?cancelled=1"
).strip()


def _is_dev_mode(secret_key: Optional[str] = None) -> bool:
    """True if we should NOT hit the live Stripe API.

    Triggers:
      - empty / missing key
      - key == "test"
      - key starts with "sk_test_" (Stripe's own test-mode prefix)

    We re-read the env on every call so tests can monkeypatch
    STRIPE_SECRET_KEY to flip the mode without re-importing the module.
    """
    k = (
        secret_key
        if secret_key is not None
        else os.getenv("STRIPE_SECRET_KEY", "test").strip()
    )
    if not k:
        return True
    if k.lower() == "test":
        return True
    if k.startswith("sk_test_"):
        return True
    return False


# Plan <-> Stripe price id lookup. In dev mode the price IDs are stubbed
# to anything-non-empty so the function can still be exercised.
PLAN_TO_PRICE = {
    "solo": STRIPE_PRICE_SOLO,
    "pro": STRIPE_PRICE_PRO,
}

PRICE_TO_PLAN = {v: k for k, v in PLAN_TO_PRICE.items() if v}


# ---------------------------------------------------------------------------
# Lazy Stripe client singleton
# ---------------------------------------------------------------------------

_client = None  # type: ignore[var-annotated]


def get_stripe_client():
    """Return a memoised ``stripe`` SDK client. None in DEV MODE."""
    global _client
    if _is_dev_mode():
        return None
    if _client is None:
        import stripe as _stripe  # local import — only paid users pay the import cost
        _stripe.api_key = STRIPE_SECRET_KEY
        _client = _stripe
    return _client


def reset_stripe_client_for_tests() -> None:
    """Test hook: drop the memoised SDK handle.

    Useful when tests toggle STRIPE_SECRET_KEY between dev and live values
    and want the next call to rebuild the client.
    """
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Public API: checkout, portal, webhook
# ---------------------------------------------------------------------------

def create_checkout_session(
    user_id: str,
    plan: str,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
) -> Dict[str, str]:
    """Create a Stripe Checkout session for ``user_id`` upgrading to ``plan``.

    Returns ``{"url": ..., "session_id": ...}``. In DEV MODE returns a
    mock URL that points at the in-app ``/api/v1/billing/mock-checkout``
    endpoint — which itself fires a synthetic webhook so the rest of the
    app exercises the same code path as production.
    """
    if plan not in PLAN_TO_PRICE:
        raise ValueError(f"unknown plan {plan!r}; expected one of {list(PLAN_TO_PRICE)}")
    success_url = success_url or STRIPE_SUCCESS_URL
    cancel_url = cancel_url or STRIPE_CANCEL_URL
    session_id = f"cs_dev_{uuid.uuid4().hex[:16]}"

    if _is_dev_mode():
        mock_url = (
            f"/api/v1/billing/mock-checkout?session_id={session_id}"
            f"&user_id={user_id}&plan={plan}"
        )
        log.info("[dev] mock checkout session: %s → %s", session_id, mock_url)
        return {"url": mock_url, "session_id": session_id}

    stripe = get_stripe_client()
    assert stripe is not None
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": PLAN_TO_PRICE[plan], "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=user_id,
        metadata={"user_id": user_id, "plan": plan},
        # We only support card today; expand later (SEPA, iDEAL, …)
        payment_method_types=["card"],
    )
    return {"url": session.url, "session_id": session.id}


def create_customer_portal_session(
    user_id: str,
    return_url: Optional[str] = None,
) -> Dict[str, str]:
    """Return ``{"url": ...}`` to Stripe's hosted Customer Portal.

    We don't store Stripe customer IDs in Phase 1.4 (the mock path doesn't
    need them); the portal link in production will be created on first
    checkout via ``customer`` field and re-used here. In dev mode we
    point the user at a tiny stub page.
    """
    return_url = return_url or STRIPE_SUCCESS_URL
    if _is_dev_mode():
        return {
            "url": (
                f"/api/v1/billing/mock-checkout?session_id=portal_dev_{uuid.uuid4().hex[:12]}"
                f"&user_id={user_id}&plan=portal"
            ),
            "session_id": f"portal_dev_{uuid.uuid4().hex[:12]}",
        }

    # Production: look up or create a Stripe customer for this user.
    stripe = get_stripe_client()
    assert stripe is not None
    user = users_db.get_user_by_id(user_id)
    if user is None:
        raise ValueError(f"unknown user_id {user_id!r}")
    customer_id = _get_or_create_customer(stripe, user_id, user["email"])
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return {"url": session.url, "session_id": session.id}


def _get_or_create_customer(stripe, user_id: str, email: str) -> str:
    """Look up an existing Stripe customer for this user, or create one.

    We store the customer id in a side-channel: a dedicated ``stripe_customer``
    column on the users table. If the column is missing (older deployments),
    we fall back to email-based lookup.
    """
    # Phase 1.4 keeps things simple: just look the customer up by email.
    customers = stripe.Customer.list(email=email, limit=1).data
    if customers:
        return customers[0].id
    return stripe.Customer.create(email=email, metadata={"user_id": user_id}).id


def verify_webhook(payload: bytes, sig_header: str) -> Dict[str, Any]:
    """Verify a Stripe webhook signature and return the parsed event dict.

    In DEV MODE we skip signature verification and just JSON-parse the
    body — webhooks fired by the mock-checkout endpoint aren't signed.
    In production we use ``stripe.Webhook.construct_event`` which raises
    ``ValueError`` on bad signature; we wrap that in a more descriptive
    ``ValueError`` so the endpoint can translate it to HTTP 400.
    """
    if _is_dev_mode():
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"dev-mode webhook body is not valid JSON: {exc}") from exc

    stripe = get_stripe_client()
    assert stripe is not None
    if not STRIPE_WEBHOOK_SECRET or STRIPE_WEBHOOK_SECRET == "test":
        # No real signing secret configured — refuse the request.
        raise ValueError("STRIPE_WEBHOOK_SECRET is not configured; refusing webhook")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as exc:
        # Stripe raises stripe.error.SignatureVerificationError but we
        # catch broadly so any failure (bad sig, malformed body, …) maps
        # to the same 400.
        raise ValueError(f"webhook signature verification failed: {exc}") from exc
    return event.to_dict() if hasattr(event, "to_dict") else dict(event)


# ---------------------------------------------------------------------------
# handle_event — single mutation path for plan upgrades/downgrades
# ---------------------------------------------------------------------------

# Event types we care about. Anything else is logged and ignored.
_HANDLED_TYPES = {
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.created",
    "customer.subscription.deleted",
    "invoice.payment_failed",
}


def handle_event(event: Dict[str, Any]) -> None:
    """Apply a verified webhook event to the local users DB.

    Rules:
      * The bootstrap admin ("lars") is NEVER downgraded. Subscription
        cancellation events for "lars" are logged and dropped.
      * checkout.session.completed sets plan from the session metadata.plan.
      * subscription.deleted / payment_failed → plan='free'.
      * subscription.updated with status='active' → look up the price in
        PRICE_TO_PLAN and set plan accordingly. status in ('past_due',
        'unpaid', 'canceled') → plan='free'.
    """
    etype = event.get("type")
    if etype not in _HANDLED_TYPES:
        log.info("[stripe] ignoring event type=%s", etype)
        return

    data = event.get("data", {}).get("object", {}) or {}
    # user_id resolution order:
    #   1. metadata.user_id (most explicit — we set this in checkout)
    #   2. client_reference_id (set by create_checkout_session)
    #   3. customer.metadata.user_id (for portal-driven events)
    user_id = (
        (data.get("metadata") or {}).get("user_id")
        or data.get("client_reference_id")
        or ((data.get("customer") or {}).get("metadata") or {}).get("user_id")
    )
    if not user_id:
        log.warning("[stripe] event %s had no user_id; ignoring", etype)
        return

    # Admin protection — never demote bootstrap admin.
    if user_id == ADMIN_USER_ID:
        log.warning(
            "[stripe] refusing to mutate admin user via %s; event dropped", etype
        )
        return

    if etype == "checkout.session.completed":
        plan = (data.get("metadata") or {}).get("plan")
        if plan not in ("solo", "pro"):
            # Fall back: infer from the line items' price id
            plan = _infer_plan_from_session(data)
        if plan in ("solo", "pro"):
            users_db.set_plan(user_id, plan)
            log.info("[stripe] checkout.session.completed → %s plan=%s", user_id, plan)
        else:
            log.warning("[stripe] could not determine plan from session: %s", data)
        return

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        status = data.get("status")
        plan = _infer_plan_from_subscription(data)
        if status == "active" and plan in ("solo", "pro"):
            users_db.set_plan(user_id, plan)
            log.info("[stripe] sub %s status=%s → %s plan=%s", etype, status, user_id, plan)
        elif status in ("past_due", "unpaid", "canceled", "incomplete_expired"):
            users_db.set_plan(user_id, "free")
            log.info("[stripe] sub %s status=%s → %s plan=free", etype, status, user_id)
        else:
            log.info("[stripe] sub %s status=%s — no change for %s", etype, status, user_id)
        return

    if etype == "customer.subscription.deleted":
        users_db.set_plan(user_id, "free")
        log.info("[stripe] sub deleted → %s plan=free", user_id)
        return

    if etype == "invoice.payment_failed":
        # Don't auto-downgrade on first failure — Stripe retries. Just log.
        log.warning("[stripe] payment_failed for %s — user stays on current plan", user_id)
        return


def _infer_plan_from_session(session: Dict[str, Any]) -> Optional[str]:
    """Pull a plan name from a checkout session's line items."""
    items = (session.get("line_items") or {}).get("data") or []
    for li in items:
        price = (li.get("price") or {})
        price_id = price.get("id")
        if price_id in PRICE_TO_PLAN:
            return PRICE_TO_PLAN[price_id]
    return None


def _infer_plan_from_subscription(sub: Dict[str, Any]) -> Optional[str]:
    items = sub.get("items", {}).get("data") or []
    for li in items:
        price = (li.get("price") or {})
        price_id = price.get("id")
        if price_id in PRICE_TO_PLAN:
            return PRICE_TO_PLAN[price_id]
    return None
