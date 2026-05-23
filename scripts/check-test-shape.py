#!/usr/bin/env python3
"""Enforce the test-shape contract on changed Python test files.

Governed by docs/contracts/07-testing.md. Runs as a pre-commit hook (pass_filenames=True).

Invariants enforced (ERROR = block commit, WARN = log only):

    TS-01 ERROR  No `.__wrapped__(` in new test files (handler-bypass anti-pattern §2.1)
    TS-02 ERROR  No SQL-substring assertions in new test files (anti-pattern §2.3)
    TS-03 ERROR  Files under tests/contract/ must declare pytest.mark.contract
    TS-04 ERROR  PI contract files must declare pytest.mark.real_auth
    TS-05 ERROR  Contract files must set loop_scope="session" on asyncio + asyncio fixtures
    TS-06 WARN   Contract tests should have at least one `# Verified: <file>:<line>` comment
    TS-07 WARN   Test files should not redefine inline `_make_pool`/`_mock_pool`/etc.

Grandfather rule: existing files that already violate TS-01/TS-02 are exempt
UNLESS the violating lines are NEW in this diff. This implements the rot-on-touch
policy: touching a file gives you the obligation to NOT add new violations,
not the obligation to clean up the legacy ones.

Usage:
    scripts/check-test-shape.py <file1> [<file2> ...]

Exit codes:
    0   All files pass (warnings printed but not blocking)
    1   One or more ERROR-level violations found

The check is scoped to files matching:
    services/*/tests/**/*.py
    libs/jarvis_common/tests/**/*.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# TS-01: handler-bypass — calling `func.__wrapped__(...)` from test code
_TS01_HANDLER_BYPASS = re.compile(r"\.__wrapped__\s*\(")

# TS-02: SQL-substring assertion — `assert ... in sql`, `assert "<KEYWORD>" in <var>`
# Pragmatic: catches the dominant shape; full SQL parser would be overkill.
_TS02_SQL_SUBSTRING = re.compile(
    r"""\bassert\b[^\n#]*\bin\b[^\n#]*\b(sql|captured_sql|query)\b"""
    r"""|\bassert\b[^\n#]*["'](SELECT|INSERT|UPDATE|DELETE|WHERE|JOIN)\b""",
    re.IGNORECASE,
)

# TS-03: contract files MUST register pytest.mark.contract in pytestmark
_TS03_CONTRACT_MARKER = re.compile(r"pytest\.mark\.contract\b")

# TS-04: PI contract files MUST register pytest.mark.real_auth in pytestmark
_TS04_REAL_AUTH = re.compile(r"pytest\.mark\.real_auth\b")

# TS-05: contract files MUST set loop_scope="session"
_TS05_LOOP_SCOPE = re.compile(r'loop_scope\s*=\s*["\']session["\']')
_TS05_ASYNCIO_FIXTURE = re.compile(r"@pytest_asyncio\.fixture\b")
_TS05_ASYNCIO_MARK_FUNC = re.compile(r"pytest\.mark\.asyncio\s*\(")

# TS-06: contract tests should cite production symbol(s)
_TS06_VERIFIED_COMMENT = re.compile(r"#\s*Verified:\s*\S+:\d+")
_TS06_DEF_TEST = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)

# TS-07: inline factories that have canonical replacements
_TS07_INLINE_FACTORIES = re.compile(
    r"^\s*(?:async\s+)?def\s+_(make_pool|mock_pool|make_embedder|build_request|"
    r"FakeRecord|make_telegram_update|make_config)\s*\(",
    re.MULTILINE,
)

# TS-07 body check for _make_config: only fire when body contains a direct
# BotConfig(...) construction (delegating wrappers that call make_bot_config
# are NOT an inline factory violation).
_TS07_BOTCONFIG_LITERAL = re.compile(r"\bBotConfig\s*\(")
_TS07_NEXT_DEF = re.compile(r"^\s*(?:async\s+)?def\s+\w", re.MULTILINE)

# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"^(services/[^/]+/tests/|libs/jarvis_common/tests/).*\.py$")
_CONTRACT_PATH_RE = re.compile(
    r"^(services/[^/]+/tests/contract/|libs/jarvis_common/tests/contract/).*\.py$"
)
_PI_CONTRACT_PATH_RE = re.compile(r"^services/paper_ingestion/tests/contract/.*\.py$")


def _is_package_init(path: str) -> bool:
    """`__init__.py` files are package markers, not test files."""
    return path.endswith("/__init__.py")


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.match(path)) and not _is_package_init(path)


def _is_contract_file(path: str) -> bool:
    return bool(_CONTRACT_PATH_RE.match(path)) and not _is_package_init(path)


def _is_pi_contract_file(path: str) -> bool:
    return bool(_PI_CONTRACT_PATH_RE.match(path)) and not _is_package_init(path)


# ---------------------------------------------------------------------------
# Grandfather: only flag NEW violations introduced by this diff
# ---------------------------------------------------------------------------


def _added_lines(path: str) -> list[tuple[int, str]]:
    """Return (line_number, content) tuples for lines added in the staged diff.

    Falls back to "all lines" if git diff fails (e.g., file is new / not in repo).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--unified=0", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        # No git in PATH → can't determine diff → treat all lines as new (CI safety)
        return _all_lines(path)
    if result.returncode != 0 or not result.stdout:
        # Unstaged: try working-tree diff vs HEAD
        result = subprocess.run(
            ["git", "diff", "--unified=0", "HEAD", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0 or not result.stdout:
        # Two cases:
        # 1. File is unchanged vs HEAD → no new lines → grandfather all legacy content.
        # 2. File is untracked (new) → treat all lines as new so anti-patterns
        #    in brand-new files are caught.
        # Distinguish by checking if the file is tracked at HEAD.
        ls_result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_result.returncode == 0:
            # File is tracked, no diff → it's unchanged → no new violations.
            return []
        # File is untracked → brand new → check the whole thing.
        return _all_lines(path)

    added: list[tuple[int, str]] = []
    current_lineno = 0
    for line in result.stdout.splitlines():
        if line.startswith("@@"):
            # @@ -old,oldlen +new,newlen @@
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                current_lineno = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append((current_lineno, line[1:]))
            current_lineno += 1
        elif not line.startswith("-"):
            current_lineno += 1
    return added


def _all_lines(path: str) -> list[tuple[int, str]]:
    try:
        with open(path, encoding="utf-8") as f:
            return list(enumerate(f.read().splitlines(), start=1))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_file(path: str) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one test file."""
    errors: list[str] = []
    warnings: list[str] = []

    if not _is_test_file(path):
        return errors, warnings

    added = _added_lines(path)
    full_text = ""
    try:
        with open(path, encoding="utf-8") as f:
            full_text = f.read()
    except OSError:
        return errors, warnings

    # TS-01 + TS-02: only flag NEW additions (rot-on-touch)
    for lineno, content in added:
        if _TS01_HANDLER_BYPASS.search(content):
            errors.append(
                f"{path}:{lineno}: TS-01 handler-bypass anti-pattern "
                f"(`.__wrapped__()` call) — see docs/contracts/07-testing.md §2.1"
            )
        if _TS02_SQL_SUBSTRING.search(content):
            errors.append(
                f"{path}:{lineno}: TS-02 SQL-substring assertion — "
                f"see docs/contracts/07-testing.md §2.3"
            )

    # TS-03..TS-06: contract-file-only checks (apply to whole file, not just diff)
    if _is_contract_file(path) and not path.endswith("conftest.py"):
        if not _TS03_CONTRACT_MARKER.search(full_text):
            errors.append(
                f"{path}: TS-03 contract file missing `pytest.mark.contract` "
                f"in pytestmark — see docs/contracts/07-testing.md §1.2"
            )
        if _is_pi_contract_file(path) and not _TS04_REAL_AUTH.search(full_text):
            errors.append(
                f"{path}: TS-04 PI contract file missing `pytest.mark.real_auth` "
                f"in pytestmark — the autouse `_default_authenticated_user` "
                f"stub would resolve session cookies as user 1. "
                f"See docs/contracts/07-testing.md §1.2"
            )
        # TS-05: if asyncio is used at all, loop_scope=session must be set
        if _TS05_ASYNCIO_FIXTURE.search(full_text) or _TS05_ASYNCIO_MARK_FUNC.search(full_text):
            if not _TS05_LOOP_SCOPE.search(full_text):
                errors.append(
                    f"{path}: TS-05 contract file uses pytest-asyncio without "
                    f'`loop_scope="session"` — will cause cross-loop errors. '
                    f"See docs/contracts/07-testing.md §1.2"
                )
        # TS-06 WARN: check at least one test has a Verified: comment
        n_tests = len(_TS06_DEF_TEST.findall(full_text))
        n_verified = len(_TS06_VERIFIED_COMMENT.findall(full_text))
        if n_tests > 0 and n_verified == 0:
            warnings.append(
                f"{path}: TS-06 contract file with {n_tests} tests has no "
                f"`# Verified: <file>:<line>` citation — grounding rule "
                f"recommends citing production symbols. "
                f"See docs/contracts/07-testing.md §1.2"
            )

    # TS-07 WARN: inline factories that have canonical replacements (new additions only)
    full_lines = full_text.splitlines()
    for lineno, content in added:
        m = _TS07_INLINE_FACTORIES.match(content)
        if not m:
            continue
        factory_name = m.group(1)
        if factory_name == "make_config":
            # Only fire when the function body directly constructs BotConfig(...).
            # Delegating wrappers (e.g. `return make_bot_config(...)`) are fine.
            # Extract body: lines after the matched line until next `def` or EOF.
            body_start = lineno  # lineno is 1-based; index = lineno (0-based next line)
            body_lines = []
            for line in full_lines[body_start:]:
                if _TS07_NEXT_DEF.match(line):
                    break
                body_lines.append(line)
            body = "\n".join(body_lines)
            if not _TS07_BOTCONFIG_LITERAL.search(body):
                continue  # delegating wrapper — not a violation
            warnings.append(
                f"{path}:{lineno}: TS-07 inline `_make_config` directly constructs "
                f"`BotConfig(...)` — use canonical replacement: "
                f"jarvis_common.testing.make_bot_config — see "
                f"docs/contracts/07-testing.md §5 + §8"
            )
        else:
            warnings.append(
                f"{path}:{lineno}: TS-07 inline `_{factory_name}` has a canonical "
                f"replacement in jarvis_common.testing — see "
                f"docs/contracts/07-testing.md §5 + §8"
            )

    return errors, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if not argv:
        return 0

    all_errors: list[str] = []
    all_warnings: list[str] = []

    for path in argv:
        # Skip files that don't exist (e.g., deleted in this commit)
        if not Path(path).exists():
            continue
        errors, warnings = check_file(path)
        all_errors.extend(errors)
        all_warnings.extend(warnings)

    if all_warnings:
        print("\n".join(f"warning: {w}" for w in all_warnings), file=sys.stderr)

    if all_errors:
        print(
            f"\n{len(all_errors)} test-shape violation(s) — see docs/contracts/07-testing.md\n",
            file=sys.stderr,
        )
        print("\n".join(f"error: {e}" for e in all_errors), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
