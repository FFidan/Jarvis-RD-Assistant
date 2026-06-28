"""Origin immutability guard tests.

No production code under services/paper_ingestion/paper_ingestion/ should
contain SQL that mutates papers.discovery_origin via UPDATE.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_no_update_papers_set_discovery_origin() -> None:
    """No production code should mutate papers.discovery_origin."""
    repo_root = Path(__file__).resolve().parents[3]
    src_root = repo_root / "services" / "paper_ingestion" / "paper_ingestion"
    assert src_root.is_dir(), f"Source root missing: {src_root}"

    # Match SET ... discovery_origin = within a bounded window (no VALUES/SELECT between).
    # Covers UPDATE papers SET ... and DO UPDATE SET ... (ON CONFLICT). The post-match
    # `\bUPDATE\b` precondition (within 200 chars) excludes INSERT column lists.
    pattern_update = re.compile(
        r"\bSET\b(?:(?!\bVALUES\b|\bSELECT\b|\bSET\b).){0,500}\bdiscovery_origin\s*=",
        re.IGNORECASE | re.DOTALL,
    )

    violations: list[tuple[Path, int, str]] = []
    for py_file in src_root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for match in pattern_update.finditer(text):
            # Verify this SET is from an UPDATE (not an INSERT's column list)
            # by checking that within 200 chars before the SET there is UPDATE or DO UPDATE
            pre = text[max(0, match.start() - 200) : match.start()]
            if not re.search(r"\bUPDATE\b", pre, re.IGNORECASE):
                continue
            line_no = text[: match.start()].count("\n") + 1
            violations.append((py_file, line_no, match.group()[:200]))

    assert not violations, (
        "Found UPDATE-discovery_origin paths (origin-immutability violation):\n"
        + "\n".join(f"  {p}:{ln}\n    {snippet}" for p, ln, snippet in violations)
    )
