"""Self-tests for scripts/check-test-shape.py.

Tests run check_file() directly by writing source to a temp file at a path
that satisfies _is_test_file() (services/*/tests/**/*.py).  Because the file
is untracked, _added_lines() falls back to all-lines mode, which is exactly
what we want for unit-testing individual checks.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# check-test-shape.py has a hyphen so it can't be imported via normal import.
# Load it as a module via importlib.
_SCRIPTS_DIR = Path(__file__).parent.parent
_MODULE_PATH = _SCRIPTS_DIR / "check-test-shape.py"

_spec = importlib.util.spec_from_file_location("check_test_shape", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None, f"Cannot locate {_MODULE_PATH}"
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_test_shape", _mod)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

check_file = _mod.check_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A relative path prefix that satisfies _is_test_file()
_TEST_PATH_PREFIX = "services/telegram_bot/tests"


def _run_check(src: str, filename: str = "test_xyz.py") -> tuple[list[str], list[str]]:
    """Write *src* to a real temp file at a test-path and call check_file().

    The file is written under the CWD so that the relative path
    'services/telegram_bot/tests/<filename>' resolves to it.
    Returns (errors, warnings).
    """
    cwd = os.getcwd()
    rel_dir = os.path.join(cwd, _TEST_PATH_PREFIX)
    os.makedirs(rel_dir, exist_ok=True)
    rel_path = os.path.join(rel_dir, filename)
    try:
        with open(rel_path, "w", encoding="utf-8") as fh:
            fh.write(src)
        # Pass the *relative* path so _is_test_file() matches it.
        check_path = f"{_TEST_PATH_PREFIX}/{filename}"
        return check_file(check_path)
    finally:
        try:
            os.remove(rel_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# TS-07 _make_config tests (new in this patch)
# ---------------------------------------------------------------------------


def test_ts_07_flags_make_config_with_botconfig_direct():
    """make_config bodies containing BotConfig(...) trigger TS-07."""
    src = """\
def _make_config():
    return BotConfig(api_key="x")
"""
    errors, warnings = _run_check(src, filename="test_make_config_direct.py")
    assert warnings, "expected TS-07 to fire on _make_config with BotConfig"
    ts07 = [w for w in warnings if "TS-07" in w and "make_config" in w]
    assert ts07, f"TS-07 make_config warning missing; got warnings={warnings}"
    assert "make_bot_config" in ts07[0], (
        f"warning should mention canonical replacement make_bot_config; got: {ts07[0]}"
    )


def test_ts_07_skips_make_config_delegating_to_helper():
    """make_config bodies that delegate to make_bot_config do NOT trigger TS-07."""
    src = """\
def _make_config(telegram_chat_id: int | None = 777):
    return make_bot_config(telegram_chat_id=telegram_chat_id)
"""
    errors, warnings = _run_check(src, filename="test_make_config_delegating.py")
    ts07 = [w for w in warnings if "TS-07" in w and "make_config" in w]
    assert not ts07, f"expected TS-07 to be skipped for delegating _make_config; got: {ts07}"


# ---------------------------------------------------------------------------
# TS-07 regression: existing factory names still fire
# ---------------------------------------------------------------------------


def test_ts_07_still_flags_make_pool():
    """Existing make_pool factories still trigger TS-07 (regression guard)."""
    src = """\
def _make_pool():
    return FakePool()
"""
    _, warnings = _run_check(src, filename="test_make_pool_regression.py")
    ts07 = [w for w in warnings if "TS-07" in w and "make_pool" in w]
    assert ts07, f"TS-07 make_pool warning missing; got warnings={warnings}"


# ---------------------------------------------------------------------------
# TS-07 regression: _make_config with multi-line BotConfig still fires
# ---------------------------------------------------------------------------


def test_ts_07_flags_make_config_multiline_botconfig():
    """Multi-line BotConfig construction in _make_config body is still caught."""
    src = """\
def _make_config() -> BotConfig:
    return BotConfig(
        telegram_token="tok",
        telegram_chat_id=123,
    )
"""
    _, warnings = _run_check(src, filename="test_make_config_multiline.py")
    ts07 = [w for w in warnings if "TS-07" in w and "make_config" in w]
    assert ts07, f"TS-07 should fire on multiline BotConfig; got warnings={warnings}"
