"""Tests for the no-growth complexity ratchet (scripts/check-complexity-budget.py)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

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


def test_measure_raises_when_ruff_tool_errors(monkeypatch):
    """A ruff tool error must abort, not silently measure zero complexity."""
    mod = _load()
    # Ruff exits 2 on a tool error (0 = clean, 1 = violations) and writes nothing
    # to stdout, so an unchecked returncode measures every rule as zero.
    errored = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom")
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: errored)

    with pytest.raises(SystemExit, match="boom"):
        mod.measure(["C901"])


def test_measure_raises_when_a_target_path_is_missing(monkeypatch):
    """A renamed or moved target must abort before ruff runs, not measure zero."""
    mod = _load()
    monkeypatch.setattr(mod, "_TARGETS", ("services", "no_such_dir"))
    monkeypatch.setattr(
        mod.subprocess, "run", lambda *args, **kwargs: pytest.fail("ruff must not be invoked")
    )

    with pytest.raises(SystemExit, match="no_such_dir"):
        mod.measure(["C901"])


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        # Ruff exits 0 with empty stdout, so the exit-2 guard passes it through and
        # every rule measures as zero — the gate then prints "Complexity improved".
        (0, "", "warning: Failed to lint services: No such file or directory (os error 2)"),
        # An unreadable path uses different wording and can still exit 1 with a
        # PARTIAL count, which reads as an improvement rather than as a failure.
        (0, "", "warning: Encountered error: Permission denied (os error 13)"),
        (
            1,
            '[{"code": "C901", "count": 4}]',
            "warning: Encountered error: Permission denied (os error 13)",
        ),
    ],
)
def test_measure_raises_when_ruff_could_not_read_a_target(monkeypatch, returncode, stdout, stderr):
    """Anything ruff failed to read must abort: it warns but still exits 0 or 1."""
    mod = _load()
    skipped = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: skipped)

    with pytest.raises(SystemExit, match="could not read every target"):
        mod.measure(["C901"])


def test_measure_reports_zero_for_a_rule_ruff_found_clean(monkeypatch):
    """A genuinely clean rule stays reportable — ruff also exits 0 with empty stdout."""
    mod = _load()
    # Unrelated ruff warnings land on stderr as well, so a guard keyed on stderr
    # being non-empty would make a real zero fatal.
    clean = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr="warning: Selection `PLR0904` has no effect because preview is not enabled.",
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: clean)

    assert mod.measure(["C901"]) == {"C901": 0}


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
