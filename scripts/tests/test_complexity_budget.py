"""Tests for the no-growth complexity ratchet (scripts/check-complexity-budget.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check-complexity-budget.py"


def _load():
    spec = importlib.util.spec_from_file_location("_complexity_budget", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_baseline_matches_live_ruff_counts():
    mod = _load()
    baseline = mod.baseline()
    assert baseline == mod.measure(sorted(baseline)), (
        "[tool.complexity-budget] drifted from live ruff output — re-freeze the caps"
    )


def test_growth_fails_and_shrink_passes(monkeypatch):
    mod = _load()
    base = mod.baseline()
    grown = dict(base)
    grown["C901"] = base["C901"] + 1
    monkeypatch.setattr(mod, "measure", lambda codes: grown)
    assert mod.main() == 1  # growth -> non-zero exit

    shrunk = dict(base)
    shrunk["C901"] = base["C901"] - 1
    monkeypatch.setattr(mod, "measure", lambda codes: shrunk)
    assert mod.main() == 0  # improvement never fails the gate (matches eslint budget)
