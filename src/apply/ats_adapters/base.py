"""ADOPT-12 base adapter contracts.

This module defines:

  - :class:`ApplyResult` — dataclass returned by every adapter. Captures
    success/screenshot/error plus two safety flags:
      * ``is_generic_fallback`` — True when a non-specialised adapter handled
        the form (the runner must never auto-submit these).
      * ``paused_for_review``  — True when the adapter stopped short of
        submitting so a human can verify before the final click.

  - :class:`Profile` — minimal profile dict shape that adapters consume.
    Parsed from ``master/cv-master.yaml`` via :func:`parse_master_profile`.

  - :class:`Driver` / :class:`FieldFiller` — minimal async Protocol that
    decouples adapters from the concrete cua-driver implementation. Tests
    pass a fake; the runner wires the real desktop driver.

  - :class:`ApplyAdapter` — Protocol every ATS-specific adapter implements.

  - :data:`ATS_REGISTRY` — name -> adapter-class dispatch table.

Safety invariant: generic fallback ALWAYS sets
``paused_for_review=True, is_generic_fallback=True``. The runner refuses
to submit when either flag is True.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent.parent  # src/apply/ats_adapters -> job-pipeline/
# Profile lookup order: explicit env override, then canonical master/cv-master.yaml,
# then a fallback at config/lars.yaml (so the existing job-pipeline config keeps
# working out-of-the-box on a fresh checkout).
_MASTER_PROFILE_PATHS: List[Path] = []  # populated lazily by _candidate_profile_paths()


def _candidate_profile_paths() -> List[Path]:
    """Build the profile lookup list in priority order.

    Order: explicit CV_MASTER_PROFILE env override, then
    ``master/cv-master.yaml`` (canonical), then ``config/lars.yaml``
    (existing job-pipeline config). Missing files are filtered out so the
    caller only ever sees paths that actually exist.
    """
    out: List[Path] = []
    env = os.getenv("CV_MASTER_PROFILE")
    if env:
        out.append(Path(env))
    out.append(_PROJECT_ROOT / "master" / "cv-master.yaml")
    out.append(_PROJECT_ROOT / "config" / "lars.yaml")
    return [p for p in out if p and p.exists()]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LoadProfileError(RuntimeError):
    """Raised when parse_master_profile cannot locate a profile."""


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """Minimal profile that adapters consume.

    Sourced from ``master/cv-master.yaml`` (ADOPT-3) or ``config/lars.yaml``.
    """

    first_name: str
    last_name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    portfolio: str = ""
    summary: str = ""

    # Free-form extras (e.g. years_experience, willing_to_relocate).
    extras: Dict[str, Any] = field(default_factory=dict)

    def display_name(self) -> str:
        # Collapse internal whitespace runs (e.g. "  Lars  " -> "Lars") so
        # the result reads naturally when we splice it into a form field.
        full = f"{self.first_name} {self.last_name}".strip()
        return " ".join(full.split())


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def parse_master_profile(path: Optional[str] = None) -> Profile:
    """Load a Profile from the first matching profile file.

    Args:
        path: Optional explicit path. If not given, walks
            ``_MASTER_PROFILE_PATHS`` in order.

    Returns:
        :class:`Profile`

    Raises:
        LoadProfileError: when no profile file is found or required
            fields are missing.
    """
    candidates: List[Path] = []
    if path:
        candidates.append(Path(path))
    candidates.extend(_candidate_profile_paths())

    raw: Optional[Mapping[str, Any]] = None
    used_path: Optional[Path] = None
    for cand in candidates:
        if cand.exists() and cand.is_file():
            try:
                text = cand.read_text(encoding="utf-8")
                data = yaml.safe_load(text)
            except (OSError, yaml.YAMLError) as exc:
                raise LoadProfileError(f"failed to read profile at {cand}: {exc}") from exc
            if not isinstance(data, Mapping):
                # The file might be a non-profile YAML (e.g. scoring config);
                # keep walking.
                continue
            # Sanity-check: a profile file MUST carry at least first_name or
            # personal.first_name (the latter is lars.yaml's structure). If it
            # has neither, treat it as non-profile and keep walking rather
            # than surfacing a missing-fields error for the wrong file.
            personal = data.get("personal", {})
            has_first = bool(data.get("first_name") or data.get("firstName") or
                             (isinstance(personal, Mapping) and personal.get("first_name")))
            if not has_first:
                continue
            raw = data
            used_path = cand
            break

    if raw is None:
        raise LoadProfileError(
            "no profile file found — checked: "
            + ", ".join(str(p) for p in candidates)
        )

    # Support both top-level keys and a nested `personal:` block (lars.yaml
    # uses the latter). We treat them as the same source.
    personal = raw.get("personal", {}) if isinstance(raw.get("personal"), Mapping) else {}

    def get(*keys: str, default: str = "") -> str:
        for k in keys:
            v = raw.get(k)
            if v:
                return _coerce_str(v, default)
            v = personal.get(k)
            if v:
                return _coerce_str(v, default)
        return default

    first_name = get("first_name", "firstName", "given_name")
    last_name = get("last_name", "lastName", "surname", "family_name")
    email = get("email", "contact_email")
    if not first_name or not last_name or not email:
        raise LoadProfileError(
            f"profile at {used_path} is missing first_name/last_name/email"
        )

    # Pull any remaining top-level scalar keys into extras so adapters can
    # reach things like `years_experience` or `willing_to_relocate`.
    reserved = {
        "first_name", "firstName", "given_name",
        "last_name", "lastName", "surname", "family_name",
        "email", "contact_email", "personal",
        "phone", "phone_number", "mobile",
        "location", "city", "country",
        "linkedin", "linkedin_url",
        "portfolio", "website", "homepage",
        "summary", "bio",
    }
    extras = {
        k: v for k, v in raw.items() if k not in reserved and not isinstance(v, Mapping)
    }

    return Profile(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=get("phone", "phone_number", "mobile"),
        location=get("location", "city", "country"),
        linkedin=get("linkedin", "linkedin_url"),
        portfolio=get("portfolio", "website", "homepage"),
        summary=get("summary", "bio"),
        extras=extras,
    )


# ---------------------------------------------------------------------------
# ApplyResult
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of a single adapter run.

    Safety flags:
      - ``is_generic_fallback``: True if a non-specialised adapter (e.g.
        :class:`GenericAdapter`) handled the form. The runner refuses to
        submit when this is True.
      - ``paused_for_review``: True when the adapter stopped short of
        submitting. The runner will set status to ``needs_human``.
    """

    success: bool
    screenshot_path: str = ""
    error: Optional[str] = None
    is_generic_fallback: bool = False
    paused_for_review: bool = False
    submitted: bool = False
    ats_type: str = "unknown"
    job_id: Optional[int] = None
    fields_filled: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def is_generic_fallback_result(result: ApplyResult) -> bool:
    """Safety predicate — True when the runner MUST NOT submit."""
    return bool(result.is_generic_fallback or result.paused_for_review)


def apply_pause_for_review(reason: str, **extra: Any) -> ApplyResult:
    """Helper used by adapters to construct a 'paused' result."""
    return ApplyResult(
        success=False,
        error=reason,
        paused_for_review=True,
        **extra,
    )


# ---------------------------------------------------------------------------
# Driver contract — minimal async Protocol so tests can mock freely.
# ---------------------------------------------------------------------------


@runtime_checkable
class FieldFiller(Protocol):
    """Subset of cua-driver used by ATS adapters.

    Concrete implementations should map these to the real desktop driver
    calls. Tests pass an in-memory fake that records what was asked of it.
    """

    async def goto(self, url: str) -> None: ...

    async def fill(self, selector: str, value: str) -> None: ...

    async def upload_file(self, selector: str, file_path: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def submit(self, selector: str) -> None: ...

    async def text(self, selector: str) -> str: ...

    async def exists(self, selector: str) -> bool: ...

    async def screenshot(self, file_path: str) -> str: ...

    async def query_all(self, selector: str) -> List[Dict[str, str]]: ...

    async def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> None: ...


@runtime_checkable
class Driver(Protocol):
    """Top-level driver exposed to adapters — wraps FieldFiller + lifecycle."""

    page: FieldFiller
    current_url: str

    async def open(self, url: str) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ApplyAdapter(Protocol):
    """Every ATS-specific adapter conforms to this contract."""

    ats_name: str

    async def apply(
        self,
        job: Mapping[str, Any],
        profile: Profile,
        cv_path: str,
        cover_letter_path: str,
        driver: Driver,
    ) -> ApplyResult: ...


# ---------------------------------------------------------------------------
# Registry — filled by ats_adapters/__init__.py after subclass imports.
# ---------------------------------------------------------------------------


ATS_REGISTRY: Dict[str, type] = {}


def register_adapter(name: str, cls: type) -> None:
    """Insert an adapter class into the dispatch table.

    Called from each adapter module's bottom to wire the registry at import
    time, keeping registration co-located with the implementation.
    """
    if not name or not isinstance(name, str):
        raise ValueError("adapter name must be a non-empty string")
    if not isinstance(cls, type):
        raise ValueError("adapter cls must be a class")
    ATS_REGISTRY[name.lower()] = cls


def resolve_adapter(ats_type: Optional[str]) -> Optional[type]:
    """Map an ATS name (case-insensitive) to its adapter class.

    Returns None when the name is unknown — the runner treats that as a
    signal to dispatch :class:`GenericAdapter` with a pause-for-review.
    """
    if not ats_type:
        return None
    return ATS_REGISTRY.get(ats_type.strip().lower())


__all__ = [
    "ATS_REGISTRY",
    "ApplyAdapter",
    "ApplyResult",
    "Driver",
    "FieldFiller",
    "LoadProfileError",
    "Profile",
    "apply_pause_for_review",
    "is_generic_fallback_result",
    "parse_master_profile",
    "register_adapter",
    "resolve_adapter",
]
