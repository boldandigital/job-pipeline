"""Plan limits + quota helpers (Phase 1.4).

Single source of truth for "what can a plan do?" — used by:

  * /api/v1/billing/me   → shows the user their limits + usage
  * /api/jobs, /api/stats → include `usage` blocks so the UI can warn
                            before the user hits the wall (no 403s in
                            Phase 1.4; we'll add hard enforcement in 1.5)

Limit semantics:
  * -1 in ``max_*`` fields means "unlimited".
  * ``auto_apply`` and ``telegram`` are booleans gating features, not
    rate-limited resources.
"""
from __future__ import annotations

from typing import Any, Dict

from src.db import users_db

# ---------------------------------------------------------------------------
# Plan limit table
# ---------------------------------------------------------------------------

PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    "free": {
        "max_jobs": 10,
        "max_cv_renders_per_day": 5,
        "auto_apply": False,
        "telegram": False,
    },
    "solo": {
        "max_jobs": 50,
        "max_cv_renders_per_day": 50,
        "auto_apply": False,
        "telegram": True,
    },
    "pro": {
        "max_jobs": -1,        # unlimited
        "max_cv_renders_per_day": -1,
        "auto_apply": True,
        "telegram": True,
    },
    "admin": {
        "max_jobs": -1,
        "max_cv_renders_per_day": -1,
        "auto_apply": True,
        "telegram": True,
    },
}

VALID_PLANS = set(PLAN_LIMITS.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_user_plan(user_id: str) -> str:
    """Return the plan name for ``user_id`` (defaults to "free").

    The bootstrap admin always returns "admin" — even if the DB row says
    otherwise (defence in depth).
    """
    from src.db.jobs_db import ADMIN_USER_ID
    if user_id == ADMIN_USER_ID:
        return "admin"
    row = users_db.get_user_by_id(user_id)
    if row is None:
        return "free"
    plan = (row["plan"] or "free").strip().lower()
    return plan if plan in PLAN_LIMITS else "free"


def get_plan_limits(plan: str) -> Dict[str, Any]:
    """Return a copy of the limits dict for ``plan`` (fallback: free)."""
    return dict(PLAN_LIMITS.get(plan, PLAN_LIMITS["free"]))


def check_jobs_quota(user_id: str, current_jobs_count: int) -> bool:
    """True if the user is still under their max_jobs limit.

    -1 = unlimited → always True.
    """
    limits = get_plan_limits(get_user_plan(user_id))
    cap = limits["max_jobs"]
    if cap < 0:
        return True
    return current_jobs_count < cap


def check_render_quota(user_id: str, renders_today: int) -> bool:
    """True if the user is still under their daily CV render limit."""
    limits = get_plan_limits(get_user_plan(user_id))
    cap = limits["max_cv_renders_per_day"]
    if cap < 0:
        return True
    return renders_today < cap


def can_auto_apply(user_id: str) -> bool:
    """True if the user's plan permits the auto-apply (CUA) feature."""
    return bool(get_plan_limits(get_user_plan(user_id))["auto_apply"])


def has_telegram(user_id: str) -> bool:
    """True if the user's plan includes Telegram alerts."""
    return bool(get_plan_limits(get_user_plan(user_id))["telegram"])


def build_usage_block(user_id: str, current_jobs_count: int = 0,
                      renders_today: int = 0) -> Dict[str, Any]:
    """Return the ``usage`` payload surfaced by /api/v1/billing/me,
    /api/jobs, and /api/stats.

    The UI uses this to render "X / 10 jobs used" or a "warning" badge
    once the user is past 80% of their cap. Hard 403s are a Phase 1.5
    concern.
    """
    plan = get_user_plan(user_id)
    limits = get_plan_limits(plan)
    jobs_cap = limits["max_jobs"]
    renders_cap = limits["max_cv_renders_per_day"]
    return {
        "plan": plan,
        "limits": limits,
        "jobs": {
            "used": current_jobs_count,
            "cap": jobs_cap,
            "unlimited": jobs_cap < 0,
            "near_limit": (jobs_cap > 0 and current_jobs_count >= int(jobs_cap * 0.8)),
            "exhausted": (jobs_cap > 0 and current_jobs_count >= jobs_cap),
        },
        "renders_today": {
            "used": renders_today,
            "cap": renders_cap,
            "unlimited": renders_cap < 0,
            "near_limit": (renders_cap > 0 and renders_today >= int(renders_cap * 0.8)),
            "exhausted": (renders_cap > 0 and renders_today >= renders_cap),
        },
    }
