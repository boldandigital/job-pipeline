"""ATS-specific application adapters.

Each adapter implements the :class:`ApplyAdapter` protocol defined in
``base.py``. The adapter is responsible for:

  1. Navigating the ATS-specific career page structure.
  2. Filling fields from a profile dict (see ``parse_master_profile``).
  3. Uploading CV + cover letter (when the ATS exposes a file input).
  4. Submitting (or pausing — generic fallback always pauses).
  5. Returning a populated :class:`ApplyResult`.

Adapters are dispatched by ATS type (greenhouse, workday, lever, ashby,
or generic fallback) from :func:`src.apply.runner.dispatch_adapter`.

Adding a new ATS:
  - Subclass :class:`ApplyAdapter` (or conform to the Protocol).
  - Implement :meth:`ApplyAdapter.apply` for the specific form structure.
  - Register the ATS name in :data:`ATS_REGISTRY` below.
  - Add a unit test in ``tests/test_apply_<name>.py`` with a mocked driver.

Note: imports are guarded so a partial phase (e.g. Phase 1 without Phase 2
adapters) can still load. The runner imports this package only after all
adapter modules are on disk.
"""

from __future__ import annotations

from .base import (
    ATS_REGISTRY,
    ApplyAdapter,
    ApplyResult,
    Driver,
    FieldFiller,
    LoadProfileError,
    Profile,
    apply_pause_for_review,
    is_generic_fallback_result,
    parse_master_profile,
)

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
]


def _try_import(name: str, attr: str):
    """Import an adapter module, tolerating missing phases during dev.

    Each phase commits its adapter to the registry on import. If a later
    phase hasn't been written yet, we just skip the import — earlier phases
    remain usable in isolation. Production runs after all phases land will
    always see the full set.
    """
    try:
        mod = __import__(f"{__name__}.{name}", fromlist=[attr])
        return getattr(mod, attr)
    except (ImportError, ModuleNotFoundError):
        return None


# Populate registry lazily — order matters: more-specific adapters win.
for _name, _attr in (
    ("greenhouse", "GreenhouseAdapter"),
    ("workday", "WorkdayAdapter"),
    ("lever", "LeverAdapter"),
    ("ashby", "AshbyAdapter"),
    ("generic", "GenericAdapter"),
):
    cls = _try_import(_name, _attr)
    if cls is not None:
        globals()[_attr] = cls
        __all__.append(_attr)
