"""Tests for the AST-based JSONB double-encode linter.

scripts/check-no-jsonb-double-encode.py walks Python source and flags any
``conn.execute(...)`` (and similar) call that passes both a ``json.dumps(...)``
arg and a SQL string containing ``::jsonb``.

Tests use tempfile.TemporaryDirectory so they leave no filesystem residue.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

# Resolve the linter module from the repo root regardless of working directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LINTER = _REPO_ROOT / "scripts" / "check-no-jsonb-double-encode.py"


def _load_linter():
    """Return the linter module (fresh load each call — avoids module-cache collisions)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_jsonb_linter", str(_LINTER))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Sanity: linter script exists and is importable
# ---------------------------------------------------------------------------


def test_linter_script_exists() -> None:
    assert _LINTER.is_file(), f"Linter not found: {_LINTER}"


# ---------------------------------------------------------------------------
# Violation fixture: json.dumps() co-located with ::jsonb in same execute call
# ---------------------------------------------------------------------------

_VIOLATION_SOURCE = """\
import json
import asyncpg

async def bad(conn, data):
    await conn.execute(
        "INSERT INTO t (col) VALUES ($1::jsonb)",
        json.dumps(data),
    )
"""

_GOOD_SOURCE = """\
import json
import asyncpg

async def good(conn, data):
    # Correct: pass native dict — asyncpg codec encodes automatically
    await conn.execute(
        "INSERT INTO t (col) VALUES ($1::jsonb)",
        data,
    )
"""

_SUPPRESSED_SOURCE = """\
import json
import asyncpg

async def suppressed(conn, data):
    await conn.execute(  # nolint:jsonb-double-encode
        "INSERT INTO t (col) VALUES ($1::jsonb)",
        json.dumps(data),
    )
"""

_MULTILINE_VIOLATION_SOURCE = """\
import json

async def distant(conn):
    # json.dumps assigned far from the execute call
    payload = json.dumps({"key": "val"})
    # Many lines later...
    sql = "UPDATE t SET col = $1::jsonb WHERE id = $2"
    await conn.execute(sql, json.dumps({"key": "val"}), 42)
"""


def _write_fixture(directory: str, name: str, source: str) -> str:
    path = Path(directory) / name
    path.write_text(source)
    return str(path)


def test_violation_detected() -> None:
    """A file with json.dumps() passed to execute(..., ::jsonb) triggers a violation."""
    mod = _load_linter()
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_fixture(tmpdir, "bad.py", _VIOLATION_SOURCE)
        violations = mod.check_file(Path(tmpdir) / "bad.py")
    assert len(violations) >= 1, "Expected at least one violation but found none"
    assert "double-encode" in violations[0][1].lower() or "jsonb" in violations[0][1].lower()


def test_good_code_passes() -> None:
    """A file passing native data to execute(..., ::jsonb) produces no violations."""
    mod = _load_linter()
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_fixture(tmpdir, "good.py", _GOOD_SOURCE)
        violations = mod.check_file(Path(tmpdir) / "good.py")
    assert violations == [], f"Unexpected violations: {violations}"


def test_suppressed_line_skipped() -> None:
    """A line with # nolint:jsonb-double-encode is not flagged."""
    mod = _load_linter()
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_fixture(tmpdir, "suppressed.py", _SUPPRESSED_SOURCE)
        violations = mod.check_file(Path(tmpdir) / "suppressed.py")
    assert violations == [], f"Suppressed violation should be skipped: {violations}"


def test_linter_main_returns_nonzero_on_violation() -> None:
    """main() returns 1 when violations are found."""
    mod = _load_linter()
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_fixture(tmpdir, "bad.py", _VIOLATION_SOURCE)
        exit_code = mod.main([tmpdir])
    assert exit_code == 1, f"Expected exit code 1, got {exit_code}"


def test_linter_main_returns_zero_on_clean_tree() -> None:
    """main() returns 0 when no violations are found."""
    mod = _load_linter()
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_fixture(tmpdir, "good.py", _GOOD_SOURCE)
        exit_code = mod.main([tmpdir])
    assert exit_code == 0, f"Expected exit code 0, got {exit_code}"


def test_linter_ignores_nonexistent_path(capsys) -> None:
    """main() warns and continues (returns 0) when a path does not exist."""
    mod = _load_linter()
    exit_code = mod.main(["/nonexistent/path/xyz"])
    assert exit_code == 0


def test_linter_passes_on_current_services_tree() -> None:
    """The current services/ and libs/ tree must be clean (self-check)."""
    mod = _load_linter()
    services = str(_REPO_ROOT / "services")
    libs = str(_REPO_ROOT / "libs")
    exit_code = mod.main([services, libs])
    assert exit_code == 0, (
        "JSONB double-encode violation found in services/ or libs/ — "
        "run scripts/check-no-jsonb-double-encode.py for details"
    )
