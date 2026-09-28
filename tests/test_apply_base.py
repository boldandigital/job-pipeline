"""Tests for the apply/ats_adapters base module.

Covers:
  - Profile dataclass (display_name, extras passthrough)
  - parse_master_profile: top-level keys, nested personal block, missing file
  - parse_master_profile: walks past non-profile YAMLs (lars.yaml)
  - parse_master_profile: explicit CV_MASTER_PROFILE override
  - ApplyResult: safety predicates (is_generic_fallback_result)
  - apply_pause_for_review: returns paused_for_review=True
  - ATS_REGISTRY round-trip (register / resolve case-insensitive)
  - Driver / FieldFiller Protocol runtime-checkable (FakePage conforms)

Run: pytest tests/test_apply_base.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# conftest.py bootstraps apply_base + apply_greenhouse modules into sys.modules.
from tests.conftest import apply_base as base_mod  # noqa: E402
from tests.conftest import FakeDriver, FakePage  # noqa: E402


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------


def test_profile_display_name_basic():
    p = base_mod.Profile(first_name="Lars", last_name="Zimmermann", email="l@x.com")
    assert p.display_name() == "Lars Zimmermann"


def test_profile_display_name_handles_whitespace():
    p = base_mod.Profile(first_name="  Lars  ", last_name=" Z ", email="l@x.com")
    assert p.display_name() == "Lars Z"


def test_profile_extras_isolated_per_instance():
    p1 = base_mod.Profile(first_name="A", last_name="B", email="x@x.com")
    p1.extras["foo"] = 1
    p2 = base_mod.Profile(first_name="A", last_name="B", email="x@x.com")
    assert "foo" not in p2.extras


# ---------------------------------------------------------------------------
# parse_master_profile
# ---------------------------------------------------------------------------


def test_parse_top_level_keys(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "first_name: Ada\nlast_name: Lovelace\nemail: ada@analytical.engine\n",
        encoding="utf-8",
    )
    p = base_mod.parse_master_profile(str(profile_path))
    assert p.first_name == "Ada"
    assert p.last_name == "Lovelace"
    assert p.email == "ada@analytical.engine"
    assert p.display_name() == "Ada Lovelace"


def test_parse_nested_personal_block(tmp_path: Path):
    """lars.yaml-style: a `personal:` block holds the name + contact info."""
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "personal:\n  first_name: Lars\n  last_name: Z\n  email: l@x.com\n"
        "  phone: '+32 0'\n  location: 'Aarschot, BE'\n",
        encoding="utf-8",
    )
    p = base_mod.parse_master_profile(str(profile_path))
    assert p.first_name == "Lars"
    assert p.phone == "+32 0"
    assert p.location == "Aarschot, BE"


def test_parse_extras_collects_unknown_top_level_keys(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "first_name: A\nlast_name: B\nemail: a@b.com\n"
        "years_experience: 12\nwilling_to_relocate: 'No'\n",
        encoding="utf-8",
    )
    p = base_mod.parse_master_profile(str(profile_path))
    assert p.extras.get("years_experience") == 12
    assert p.extras.get("willing_to_relocate") == "No"


def test_parse_drops_nested_mapping_from_extras(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "first_name: A\nlast_name: B\nemail: a@b.com\n"
        "search:\n  sites: [indeed]\n",
        encoding="utf-8",
    )
    p = base_mod.parse_master_profile(str(profile_path))
    assert "search" not in p.extras


def test_parse_walks_past_non_profile_yaml(tmp_path, monkeypatch):
    """When the first candidate isn't a profile, the loader keeps walking."""
    master_dir = tmp_path / "master"
    master_dir.mkdir()
    (master_dir / "cv-master.yaml").write_text(
        "search:\n  sites: [indeed]\n", encoding="utf-8",
    )
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    profile_path = cfg_dir / "lars.yaml"
    profile_path.write_text(
        "first_name: Lars\nlast_name: Z\nemail: l@x.com\n", encoding="utf-8",
    )
    monkeypatch.setattr(base_mod, "_PROJECT_ROOT", tmp_path)

    p = base_mod.parse_master_profile()
    assert p.first_name == "Lars"
    assert p.email == "l@x.com"


def test_parse_missing_file_raises(tmp_path, monkeypatch):
    """When no candidate path resolves, the loader raises LoadProfileError."""
    empty_root = tmp_path / "empty_repo"
    empty_root.mkdir()
    monkeypatch.setattr(base_mod, "_PROJECT_ROOT", empty_root)
    with pytest.raises(base_mod.LoadProfileError) as excinfo:
        base_mod.parse_master_profile()
    assert "no profile file found" in str(excinfo.value)


def test_parse_invalid_yaml_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("first_name: : :\n  - not valid", encoding="utf-8")
    with pytest.raises(base_mod.LoadProfileError):
        base_mod.parse_master_profile(str(bad))


def test_parse_explicit_env_override(tmp_path, monkeypatch):
    explicit = tmp_path / "override.yaml"
    explicit.write_text(
        "first_name: Override\nlast_name: Path\nemail: o@p.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CV_MASTER_PROFILE", str(explicit))
    p = base_mod.parse_master_profile()
    assert p.first_name == "Override"


# ---------------------------------------------------------------------------
# ApplyResult + safety predicates
# ---------------------------------------------------------------------------


def test_apply_result_defaults_are_safe():
    r = base_mod.ApplyResult(success=True)
    assert r.is_generic_fallback is False
    assert r.paused_for_review is False
    assert r.submitted is False
    assert r.ats_type == "unknown"
    assert r.job_id is None


def test_is_generic_fallback_true_when_paused():
    r = base_mod.ApplyResult(success=False, paused_for_review=True)
    assert base_mod.is_generic_fallback_result(r)


def test_is_generic_fallback_true_when_generic_flag_set():
    r = base_mod.ApplyResult(success=False, is_generic_fallback=True)
    assert base_mod.is_generic_fallback_result(r)


def test_is_generic_fallback_false_for_real_submission():
    r = base_mod.ApplyResult(success=True, submitted=True)
    assert not base_mod.is_generic_fallback_result(r)


def test_apply_pause_for_review_helper_sets_paused():
    r = base_mod.apply_pause_for_review("login wall", ats_type="greenhouse",
                                          job_id=42)
    assert r.paused_for_review is True
    assert r.ats_type == "greenhouse"
    assert r.job_id == 42
    assert "login wall" in (r.error or "")


def test_apply_result_to_dict_serialisable():
    r = base_mod.ApplyResult(success=True, screenshot_path="/x.png",
                              ats_type="lever", job_id=99)
    d = r.to_dict()
    assert d["success"] is True
    assert d["ats_type"] == "lever"
    assert d["fields_filled"] == []


# ---------------------------------------------------------------------------
# Registry + protocol conformance
# ---------------------------------------------------------------------------


def test_registry_register_and_resolve_case_insensitive():
    class _Stub:
        ats_name = "stub-ats"

    base_mod.register_adapter("Stub-ATS", _Stub)
    assert base_mod.resolve_adapter("stub-ats") is _Stub
    assert base_mod.resolve_adapter("STUB-ATS") is _Stub
    base_mod.ATS_REGISTRY.pop("stub-ats")


def test_registry_resolve_unknown_returns_none():
    assert base_mod.resolve_adapter(None) is None
    assert base_mod.resolve_adapter("") is None
    assert base_mod.resolve_adapter("does-not-exist") is None


def test_register_rejects_invalid_name():
    with pytest.raises(ValueError):
        base_mod.register_adapter("", object)
    with pytest.raises(ValueError):
        base_mod.register_adapter(123, object)  # type: ignore[arg-type]


def test_register_rejects_non_class():
    with pytest.raises(ValueError):
        base_mod.register_adapter("nope", "not a class")  # type: ignore[arg-type]


def test_fake_page_conforms_to_field_filler_protocol():
    page = FakePage()
    assert isinstance(page, base_mod.FieldFiller)


def test_fake_driver_conforms_to_driver_protocol():
    d = FakeDriver()
    assert isinstance(d, base_mod.Driver)
