"""In-memory token-bucket rate limiter (Phase 1.2 — signups).

Keyed by client IP. Buckets refill continuously. State lives in module-level
dicts (single-process — fine for dev and single-worker prod).

For multi-worker prod, swap the dict state for Redis without touching callers.

Usage:
    from src.web.rate_limit import rate_limit_signup

    allowed, retry_after = rate_limit_signup(request.client.host)
    if not allowed:
        raise HTTPException(429, ...)
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Tuple

# -------------------------------------------------------------------
# Signup bucket: max 5 per IP per hour (sliding window)
# -------------------------------------------------------------------

_SIGNUP_WINDOW = 60 * 60  # 1 hour
_SIGNUP_LIMIT = 5

# IP → deque[float] of request timestamps
_signup_log: dict[str, Deque[float]] = {}
_lock = threading.Lock()


def _prune(dq: Deque[float], now: float, window: float) -> None:
    while dq and (now - dq[0]) > window:
        dq.popleft()


def rate_limit_signup(ip: str) -> Tuple[bool, int]:
    """Returns (allowed, retry_after_seconds).

    retry_after_seconds is the time the caller must wait before the next
    allowed request. 0 when allowed.
    """
    if not ip:
        ip = "unknown"
    now = time.time()
    with _lock:
        dq = _signup_log.setdefault(ip, deque())
        _prune(dq, now, _SIGNUP_WINDOW)
        if len(dq) >= _SIGNUP_LIMIT:
            # The oldest entry will expire at dq[0] + window
            retry = max(1, int(dq[0] + _SIGNUP_WINDOW - now))
            return False, retry
        dq.append(now)
        return True, 0


def _reset_for_tests() -> None:
    """Wipe the in-memory bucket. Tests call this to start each case clean."""
    with _lock:
        _signup_log.clear()