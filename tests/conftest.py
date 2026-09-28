"""Shared pytest fixtures for the apply pipeline.

A :class:`FakeDriver` / :class:`FakePage` pair that records every call made
against it. Adapters run against this fake instead of a real browser, so the
test suite is hermetic and fast.

Selectors are matched against an in-memory DOM dict. Tests seed the DOM
with the structure the adapter expects (e.g. ``#first_name: <input>``) and
assert against the recorded call log.

The package loader glue (so adapter modules can use relative imports during
test collection) lives here too.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Bootstrap the apply.* package tree so relative imports work during tests.
# We register the package and load base.py + greenhouse.py explicitly so the
# greenhouse tests can run before Phase 2/3 adapter modules are on disk.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
_APPLY_PKG_PATH = ROOT / "src" / "apply"
_ADAPTERS_PKG_PATH = _APPLY_PKG_PATH / "ats_adapters"


def _load_pkg(pkg: str, pkg_path: Path):
    spec = importlib.util.spec_from_file_location(
        pkg, pkg_path / "__init__.py",
        submodule_search_locations=[str(pkg_path)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_pkg_member(pkg: str, member: str, file_path: str):
    full = f"{pkg}.{member}"
    spec = importlib.util.spec_from_file_location(full, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load_pkg("src", ROOT / "src")
_load_pkg("src.apply", _APPLY_PKG_PATH)
_load_pkg("src.apply.ats_adapters", _ADAPTERS_PKG_PATH)
apply_base = _load_pkg_member(
    "src.apply.ats_adapters", "base", str(_ADAPTERS_PKG_PATH / "base.py"),
)
apply_greenhouse = _load_pkg_member(
    "src.apply.ats_adapters", "greenhouse",
    str(_ADAPTERS_PKG_PATH / "greenhouse.py"),
)
apply_workday = _load_pkg_member(
    "src.apply.ats_adapters", "workday",
    str(_ADAPTERS_PKG_PATH / "workday.py"),
)
apply_lever = _load_pkg_member(
    "src.apply.ats_adapters", "lever",
    str(_ADAPTERS_PKG_PATH / "lever.py"),
)
apply_ashby = _load_pkg_member(
    "src.apply.ats_adapters", "ashby",
    str(_ADAPTERS_PKG_PATH / "ashby.py"),
)
apply_generic = _load_pkg_member(
    "src.apply.ats_adapters", "generic",
    str(_ADAPTERS_PKG_PATH / "generic.py"),
)


# ---------------------------------------------------------------------------
# Fake page + driver
# ---------------------------------------------------------------------------


@dataclass
class CallLog:
    """Append-only log of driver calls — one entry per call."""
    goto: List[str] = field(default_factory=list)
    fill: List[Dict[str, str]] = field(default_factory=list)
    upload_file: List[Dict[str, str]] = field(default_factory=list)
    click: List[str] = field(default_factory=list)
    submit: List[str] = field(default_factory=list)
    text: List[str] = field(default_factory=list)
    exists_calls: List[str] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    query_all: List[str] = field(default_factory=list)
    wait_for_selector: List[str] = field(default_factory=list)


@dataclass
class FakePage:
    """In-memory DOM + call recorder. Selectors match against ``dom``."""

    dom: Dict[str, Dict[str, str]] = field(default_factory=dict)
    body_text: str = ""
    log: CallLog = field(default_factory=CallLog)
    submit_should_raise: bool = False

    # --- FieldFiller surface ------------------------------------------------

    async def goto(self, url: str) -> None:
        self.log.goto.append(url)

    async def fill(self, selector: str, value: str) -> None:
        self.log.fill.append({"selector": selector, "value": value})
        if selector in self.dom:
            self.dom[selector]["value"] = value

    async def upload_file(self, selector: str, file_path: str) -> None:
        self.log.upload_file.append({"selector": selector, "file_path": file_path})
        if selector in self.dom:
            self.dom[selector]["uploaded"] = file_path

    async def click(self, selector: str) -> None:
        self.log.click.append(selector)
        if self.submit_should_raise:
            raise RuntimeError("simulated submit failure")

    async def submit(self, selector: str) -> None:
        self.log.submit.append(selector)
        if self.submit_should_raise:
            raise RuntimeError("simulated submit failure")

    async def text(self, selector: str) -> str:
        self.log.text.append(selector)
        if selector == "body":
            return self.body_text
        if selector in self.dom:
            return self.dom[selector].get("label", "")
        return ""

    async def exists(self, selector: str) -> bool:
        self.log.exists_calls.append(selector)
        return self._matches(selector)

    def _matches(self, selector: str) -> bool:
        """Match a selector against the seeded DOM.

        Supports:
          - Exact key match: ``#first_name`` matches ``"#first_name"``.
          - CSS attribute-prefix: ``textarea[id^="question_"]`` matches any
            DOM key starting with ``question_``.
          - CSS attribute-substring: ``input[data-automation-id*="legal"]``
            matches DOM keys containing ``legal`` (substring inside attr).
          - ID-prefix lookup: ``#question_linkedin`` matches the DOM key
            ``"question_linkedin"`` (since the seed convention drops the #).
          - Comma-separated alternation: ``a, b, c`` is an OR of any clause.
          - Whitespace tolerance.
        """
        clauses = [c.strip() for c in selector.split(",") if c.strip()]
        if not clauses:
            return False
        for clause in clauses:
            if clause in self.dom:
                return True
            # `#some_id` -> look up `some_id` as a key.
            if clause.startswith("#") and clause[1:] in self.dom:
                return True
            # CSS prefix selector e.g. textarea[id^="question_"]
            if "[id^=" in clause:
                try:
                    prefix = clause.split('"')[1]
                except IndexError:
                    prefix = ""
                if prefix and any(k.startswith(prefix) for k in self.dom):
                    return True
            # CSS substring selector e.g. input[data-automation-id*="legal"]
            if "[*=" in clause:
                try:
                    needle = clause.split('"')[1]
                except IndexError:
                    needle = ""
                if needle and any(needle in k for k in self.dom):
                    return True
            # Tag + attribute selector e.g. input[type="submit"]
            if clause.startswith("input[") and clause in self.dom:
                return True
        return False

    async def screenshot(self, file_path: str) -> str:
        self.log.screenshots.append(file_path)
        return file_path

    async def query_all(self, selector: str) -> List[Dict[str, str]]:
        self.log.query_all.append(selector)
        # Generic query: any comma-separated CSS selector list. Return
        # one synthetic entry per DOM key that matches at least one clause.
        clauses = [c.strip() for c in selector.split(",") if c.strip()]
        matches: List[Dict[str, str]] = []
        seen: set = set()
        for clause in clauses:
            # CSS prefix selector e.g. textarea[id^="question_"]
            if "[id^=" in clause:
                try:
                    prefix = clause.split('"')[1]
                except IndexError:
                    prefix = ""
                for k, v in self.dom.items():
                    if k.startswith(prefix) and k not in seen:
                        matches.append({"id": k, "label": v.get("label", "")})
                        seen.add(k)
                continue
            # CSS substring selector e.g. input[*="x"]
            if "[*=" in clause:
                try:
                    needle = clause.split('"')[1]
                except IndexError:
                    needle = ""
                for k, v in self.dom.items():
                    if needle and needle in k and k not in seen:
                        matches.append({"id": k, "label": v.get("label", "")})
                        seen.add(k)
                continue
            # `tag[type="x"]` exact match
            if clause in self.dom:
                matches.append({
                    "id": clause,
                    "label": self.dom[clause].get("label", ""),
                })
                seen.add(clause)
        return matches

    async def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> None:
        self.log.wait_for_selector.append(selector)
        if selector not in self.dom:
            raise RuntimeError(f"timeout waiting for {selector}")


@dataclass
class FakeDriver:
    page: FakePage = field(default_factory=FakePage)
    current_url: str = ""

    async def open(self, url: str) -> None:
        self.current_url = url
        await self.page.goto(url)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Greenhouse DOM fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def greenhouse_dom() -> Dict[str, Dict[str, str]]:
    """A minimal Greenhouse application form."""
    return {
        "#first_name":  {"tag": "input", "type": "text"},
        "#last_name":   {"tag": "input", "type": "text"},
        "#email":       {"tag": "input", "type": "email"},
        "#phone":       {"tag": "input", "type": "tel"},
        "input#resume": {"tag": "input", "type": "file"},
        "input#cover_letter": {"tag": "input", "type": "file"},
        'input[type="submit"][name="submit"]': {"tag": "input", "type": "submit"},
        'button[type="submit"]#submit': {"tag": "button", "type": "submit"},
    }


@pytest.fixture
def greenhouse_driver(greenhouse_dom: Dict[str, Dict[str, str]]) -> FakeDriver:
    d = FakeDriver()
    d.page.dom = greenhouse_dom
    return d


@pytest.fixture
def tmp_cv(tmp_path: Path) -> str:
    """Create a stub CV file the adapter can upload."""
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-stub")
    return str(p)


@pytest.fixture
def tmp_cover_letter(tmp_path: Path) -> str:
    p = tmp_path / "anschreiben.pdf"
    p.write_bytes(b"%PDF-stub")
    return str(p)


@pytest.fixture
def base_job() -> Dict[str, Any]:
    return {
        "id": 123,
        "title": "Head of Engineering",
        "company": "Stripe",
        "career_url": "https://boards.greenhouse.io/stripe",
        "url": "https://stripe.com/jobs/123",
        "ats_type": "greenhouse",
    }


@pytest.fixture
def profile():
    return apply_base.Profile(
        first_name="Lars",
        last_name="Zimmermann",
        email="lars.z@icloud.com",
        phone="+32 470 123 456",
        location="Aarschot, Belgium",
        linkedin="https://linkedin.com/in/lars-z",
        portfolio="https://boldandigital.com",
        extras={"years_experience": 12, "willing_to_relocate": "Yes"},
    )


@pytest.fixture
def adapter():
    return apply_greenhouse.GreenhouseAdapter()
