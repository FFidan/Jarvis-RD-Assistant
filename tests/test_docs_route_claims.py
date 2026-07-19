"""Docs-parity test for docs/manual/access-modes.md vs. scripts/setup_lib.sh route_claims.

route_claims() (scripts/setup_lib.sh) is the single source of truth for the
per-route transport, token-handoff, and WebAuthn contract JARVIS actually
ships. access-modes.md carries a marker-delimited copy of that table for
operators. This test parses both and asserts SET-equality row by row, so the
docs can never silently claim a tier, scheme, or cookie/passkey behaviour the
code does not grant.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETUP_LIB = _REPO_ROOT / "scripts" / "setup_lib.sh"
_ACCESS_MODES_DOC = _REPO_ROOT / "docs" / "manual" / "access-modes.md"

_COLUMNS = (
    "route",
    "scheme",
    "port",
    "host_allowlist",
    "setup_token_transport",
    "cookie_policy",
    "passkey_origin",
    "cert_owner",
    "tier",
)

_BEGIN_MARKER = "<!-- route-claims:begin -->"
_END_MARKER = "<!-- route-claims:end -->"


def _route_claims_rows() -> set[tuple[str, ...]]:
    """Run route_claims() via the real shell function and parse its pipe rows."""
    result = subprocess.run(
        ["bash", "-c", f"source {_SETUP_LIB}; route_claims"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    )
    rows: set[tuple[str, ...]] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        cols = tuple(c.strip() for c in line.split("|"))
        assert len(cols) == len(_COLUMNS), (
            f"route_claims row has {len(cols)} columns, expected {len(_COLUMNS)}: {line!r}"
        )
        rows.add(cols)
    return rows


def _doc_marker_block() -> str:
    text = _ACCESS_MODES_DOC.read_text(encoding="utf-8")
    begin = text.find(_BEGIN_MARKER)
    end = text.find(_END_MARKER)
    assert begin != -1 and end != -1 and end > begin, (
        "marker block not found: docs/manual/access-modes.md must contain a "
        f"{_BEGIN_MARKER} ... {_END_MARKER} delimited table"
    )
    return text[begin + len(_BEGIN_MARKER) : end]


def _doc_claims_rows() -> set[tuple[str, ...]]:
    """Parse the markdown table inside the marker block into column tuples.

    Skips the header row and the `|---|---|...` separator row; every
    remaining `| a | b | ... |` row is split into stripped cell values.
    """
    block = _doc_marker_block()
    rows: set[tuple[str, ...]] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0].lower() == _COLUMNS[0] or re.fullmatch(r"-+", cells[0]):
            continue  # header row or `---` separator row
        assert len(cells) == len(_COLUMNS), (
            f"docs route-claims row has {len(cells)} columns, expected {len(_COLUMNS)}: {line!r}"
        )
        rows.add(tuple(cells))
    return rows


def test_access_modes_route_claims_table_matches_setup_lib() -> None:
    """The marker-delimited table in access-modes.md must equal route_claims() exactly."""
    code_rows = _route_claims_rows()
    doc_rows = _doc_claims_rows()

    assert doc_rows, "docs route-claims table is empty — every route_claims() route must be listed"
    missing_from_docs = code_rows - doc_rows
    extra_in_docs = doc_rows - code_rows
    assert not missing_from_docs, (
        f"route_claims() rows missing from access-modes.md: {sorted(missing_from_docs)}"
    )
    assert not extra_in_docs, (
        f"access-modes.md claims routes/values route_claims() does not grant: "
        f"{sorted(extra_in_docs)}"
    )
