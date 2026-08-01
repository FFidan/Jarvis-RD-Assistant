"""Live-schema coverage for the full-digest rewrite of locally uploaded paper ids.

The rewrite runs against ``papers.external_id``, which carries a UNIQUE
constraint, so it needs a real schema to be meaningful — the mocked upload
suites have none.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_MIGRATION_SQL = (
    Path(__file__).resolve().parents[4] / "db" / "migrations" / "0111_full_digest_local_ids.sql"
).read_text(encoding="utf-8")


async def _insert_local_paper(conn, external_id: str, url: str, title: str) -> int:
    return await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ($1, 'local', $2, ARRAY['Au'], $3)
           RETURNING id""",
        external_id,
        title,
        url,
    )


# Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:383
# Verified: services/paper_ingestion/paper_ingestion/services/local_pdfs.py:86
async def test_short_id_is_rewritten_to_the_full_digest(contract_conn):
    """A paper uploaded before the change gets the id a re-upload now computes."""
    digest = hashlib.sha256(b"%PDF-1.4 pre-existing").hexdigest()
    paper_id = await _insert_local_paper(
        contract_conn, f"local:{digest[:16]}", f"local://{digest}", "Pre-existing upload"
    )

    await contract_conn.execute(_MIGRATION_SQL)

    rewritten = await contract_conn.fetchval(
        "SELECT external_id FROM papers WHERE id = $1", paper_id
    )
    assert rewritten == f"local:{digest}", (
        "a re-upload of the same bytes must dedupe against the migrated row"
    )


async def test_rewrite_is_skipped_when_the_full_digest_id_already_exists(contract_conn):
    """The uniqueness guard keeps a pre-duplicated pair from failing the migration."""
    digest = hashlib.sha256(b"%PDF-1.4 already duplicated").hexdigest()
    short_id = await _insert_local_paper(
        contract_conn, f"local:{digest[:16]}", f"local://{digest}", "Short form"
    )
    full_id = await _insert_local_paper(
        contract_conn, f"local:{digest}", f"local://{digest}", "Full form"
    )

    await contract_conn.execute(_MIGRATION_SQL)

    assert (
        await contract_conn.fetchval("SELECT external_id FROM papers WHERE id = $1", short_id)
        == f"local:{digest[:16]}"
    ), "the short-form row must be left alone rather than collide"
    assert (
        await contract_conn.fetchval("SELECT external_id FROM papers WHERE id = $1", full_id)
        == f"local:{digest}"
    )
