"""Billing subsystem for CaptainApply (Phase 1.4).

Modules:
  - stripe_client: lazy Stripe SDK singleton, dev/mock mode, webhook
    verification + event handling.
  - quota:        plan limit dict + helpers used by /api/jobs, /api/stats
    to soft-warn (not block) users approaching their quota.

DEV MODE (no live Stripe calls):
  When STRIPE_SECRET_KEY is missing, equals "test", or starts with
  ``sk_test_``, the client returns a mock checkout URL pointing at the
  local ``/api/v1/billing/mock-checkout`` endpoint. The mock endpoint
  in turn synthesises a ``checkout.session.completed`` event and feeds
  it through the same handle_event() path a real webhook would use —
  so the rest of the app cannot tell dev from prod.
"""
