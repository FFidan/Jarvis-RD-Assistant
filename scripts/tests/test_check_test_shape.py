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
check_contract_doc = _mod.check_contract_doc

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
    _, warnings = _run_check(src, filename="test_make_config_direct.py")
    assert warnings, "expected TS-07 to fire on _make_config with BotConfig"
    ts07 = [w for w in warnings if "TS-07" in w and "make_config" in w]
    assert ts07, f"TS-07 make_config warning missing; got warnings={warnings}"
    assert "make_bot_config" in ts07[0], (
        f"warning should mention canonical replacement make_bot_config; got: {ts07[0]}"
    )


def test_ts_07_skips_make_config_delegating_to_helper():
    """make_config bodies that delegate to make_bot_config do NOT trigger TS-07."""
    src = """\
def _make_config():
    return make_bot_config()
"""
    _, warnings = _run_check(src, filename="test_make_config_delegating.py")
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
        jarvis_base_url="https://example.test",
    )
"""
    _, warnings = _run_check(src, filename="test_make_config_multiline.py")
    ts07 = [w for w in warnings if "TS-07" in w and "make_config" in w]
    assert ts07, f"TS-07 should fire on multiline BotConfig; got warnings={warnings}"


# ---------------------------------------------------------------------------
# TS-08: carve-out registry integrity in docs/contracts/07-testing.md
# ---------------------------------------------------------------------------


def _run_contract_doc_check(
    content: str,
    tmp_path: Path,
    path: str = "docs/contracts/07-testing.md",
) -> tuple[list[str], list[str]]:
    """Write *content* under *tmp_path* and call check_contract_doc().

    check_contract_doc() identifies the testing contract by normalised path
    SUFFIX, so writing to ``tmp_path/<path>`` (which still ends with
    ``docs/contracts/07-testing.md``) fires the suffix match while never
    touching the real tracked contract doc under the repo CWD. pytest cleans
    up tmp_path, so no manual removal is needed.
    """
    abs_path = tmp_path / path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return check_contract_doc(str(abs_path))


# Minimal valid testing-contract doc with ≥3 carve-out entries.
_VALID_CONTRACT_DOC = """\
# 07 — Testing Contract

## 4. Invariants

| ID | Invariant | Level |
|---|---|---|
| TS-08 | Carve-out registry must stay | ERROR |

## 5. Carve-out registry

### 5.1 Network / process boundaries

| Boundary | Mock mechanism | Test population guarded |
|---|---|---|
| Ollama HTTP | AsyncMock | ~30 residual |
| Qdrant client | MagicMock | ~25 residual |
| AsyncOpenAI / LiteLLM | MagicMock | ~15 residual |
| Telegram Bot API | AsyncMock | ~120 tests |

## 6. Next section
"""

# Contract doc with the carve-out section heading stripped entirely.
_STRIPPED_HEADING_DOC = """\
# 07 — Testing Contract

## 4. Invariants

Some text about invariants.

## 6. Next section (carve-out section removed)
"""

# Contract doc with the heading present but only 1 data row (too few entries).
_WEAKENED_DOC = """\
# 07 — Testing Contract

## 5. Carve-out registry

### 5.1 Network / process boundaries

| Boundary | Mock mechanism | Test population guarded |
|---|---|---|
| Only one entry left | MagicMock | ~1 test |

## 6. Next section
"""


def test_ts08_passes_on_intact_doc(tmp_path):
    """Intact doc with ≥3 carve-out entries: no errors."""
    errors, warnings = _run_contract_doc_check(_VALID_CONTRACT_DOC, tmp_path)
    ts08 = [e for e in errors if "TS-08" in e]
    assert not ts08, f"Expected no TS-08 errors on intact doc; got: {ts08}"
    assert not warnings, f"Unexpected warnings: {warnings}"


def test_ts08_errors_on_missing_section_heading(tmp_path):
    """Stripped carve-out heading → TS-08 ERROR."""
    errors, _ = _run_contract_doc_check(_STRIPPED_HEADING_DOC, tmp_path)
    ts08 = [e for e in errors if "TS-08" in e]
    assert ts08, f"Expected TS-08 error when heading is missing; got errors={errors}"
    assert "Carve-out registry" in ts08[0] or "## 5." in ts08[0], (
        f"Error should mention the missing heading; got: {ts08[0]}"
    )


def test_ts08_errors_on_weakened_registry(tmp_path):
    """Carve-out section with only 1 data entry → TS-08 ERROR."""
    errors, _ = _run_contract_doc_check(_WEAKENED_DOC, tmp_path)
    ts08 = [e for e in errors if "TS-08" in e]
    assert ts08, f"Expected TS-08 error when registry is weakened; got errors={errors}"
    assert "minimum" in ts08[0] or "entry" in ts08[0], (
        f"Error should mention minimum entry count; got: {ts08[0]}"
    )


def test_ts08_ignores_non_contract_files(tmp_path):
    """check_contract_doc must be a no-op for any path that is NOT 07-testing.md."""
    errors, warnings = _run_contract_doc_check(
        _STRIPPED_HEADING_DOC,
        tmp_path,
        path="docs/contracts/08-other-doc.md",
    )
    assert not errors, f"check_contract_doc should ignore non-contract paths; got: {errors}"
    assert not warnings


def test_main_no_args_scans_default_paths(monkeypatch, tmp_path):
    """CI/Makefile no-arg invocation must not silently skip all checks."""
    checked: list[str] = []
    contract_checked: list[str] = []
    test_path = tmp_path / "services" / "paper_ingestion" / "tests" / "test_example.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_example():\n    assert True\n", encoding="utf-8")

    monkeypatch.setattr(_mod, "_default_paths", lambda: [str(test_path)])

    def fake_check_file(path: str) -> tuple[list[str], list[str]]:
        checked.append(path)
        return [], []

    def fake_check_contract_doc(path: str) -> tuple[list[str], list[str]]:
        contract_checked.append(path)
        return [], []

    monkeypatch.setattr(_mod, "check_file", fake_check_file)
    monkeypatch.setattr(_mod, "check_contract_doc", fake_check_contract_doc)

    assert _mod.main([]) == 0
    assert checked == [str(test_path)]
    assert contract_checked == [str(test_path)]


def test_default_paths_selects_tracked_tests_and_contracts(monkeypatch):
    """Default discovery uses tracked test/contract files and excludes unrelated paths."""

    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "README.md",
                "libs/jarvis_common/tests/test_email.py",
                "services/paper_ingestion/tests/test_reranker_loadfail.py",
                "docs/contracts/07-testing.md",
                "docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl",
            ]
        )
        stderr = ""

    monkeypatch.setattr(_mod.subprocess, "run", lambda *args, **kwargs: Result())

    assert _mod._default_paths() == [
        "libs/jarvis_common/tests/test_email.py",
        "services/paper_ingestion/tests/test_reranker_loadfail.py",
        "docs/contracts/07-testing.md",
    ]


def test_main_no_args_fails_when_default_paths_empty(monkeypatch, capsys):
    """No-arg invocation must fail closed instead of passing without checks."""

    monkeypatch.setattr(_mod, "_default_paths", lambda: [])

    assert _mod.main([]) == 1
    assert "no default files discovered" in capsys.readouterr().err


def test_default_paths_returns_empty_on_git_failure(monkeypatch):
    """A git discovery failure is visible to main() as an empty fail-closed set."""

    class Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(_mod.subprocess, "run", lambda *args, **kwargs: Result())

    assert _mod._default_paths() == []
