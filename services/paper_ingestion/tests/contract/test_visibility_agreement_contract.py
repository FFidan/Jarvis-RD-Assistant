"""Real-PostgreSQL agreement test for the centralized visibility predicate.

Single-paper authorization and bulk SQL must grant exactly persisted public
scope or explicit caller-library membership. Source labels and discoverer audit
values are deliberately varied to prove they have no authority.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException

from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.testing import seed_user_row
from paper_ingestion.ingestion.embed_store import EmbeddingStoreMixin
from paper_ingestion.ingestion.search import EmbeddingSearchMixin
from paper_ingestion.queries.predicates import paper_visible_sql

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_paper(
    conn,
    external_id: str,
    visibility_scope: str = "private",
    discovered_by: int | None = None,
    source_type: str = "arxiv",
) -> int:
    """Insert one paper with explicit scope and independent audit provenance."""
    return int(
        await conn.fetchval(
            """INSERT INTO papers (
                   external_id, source_type, title, authors, url,
                   discovered_by, visibility_scope
               )
               VALUES ($1, $4, 'Visibility paper', ARRAY['A. Author'],
                       'https://example.test/visibility', $2, $3)
               RETURNING id""",
            external_id,
            discovered_by,
            visibility_scope,
            source_type,
        )
    )


async def _add_to_library(conn, user_id: int, paper_id: int) -> None:
    """Place *paper_id* into *user_id*'s library."""
    await conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        paper_id,
    )


_SEEDED_PDF_PATH = "/data/pdfs/seeded.pdf"
_SEEDED_CHUNK_COUNT = 2


async def _seed_derived_pdf_content(conn, paper_id: int) -> None:
    """Give *paper_id* the derived state a completed PDF run leaves behind."""
    await conn.executemany(
        "INSERT INTO paper_chunks (paper_id, chunk_index, content) VALUES ($1, $2, $3)",
        [(paper_id, index, f"chunk {index}") for index in range(_SEEDED_CHUNK_COUNT)],
    )
    await conn.execute(
        """UPDATE papers
              SET pdf_downloaded = TRUE, pdf_local_path = $2, chunked_at = now()
            WHERE id = $1""",
        paper_id,
        _SEEDED_PDF_PATH,
    )


async def _derived_pdf_content(conn, paper_id: int):
    """Return the persisted PDF-derived columns plus the paper's chunk-row count."""
    return await conn.fetchrow(
        """SELECT p.pdf_downloaded, p.pdf_local_path, p.chunked_at,
                  (SELECT COUNT(*) FROM paper_chunks c WHERE c.paper_id = p.id) AS chunk_count
             FROM papers p
            WHERE p.id = $1""",
        paper_id,
    )


def _assert_derived_pdf_content_intact(state, cell: str) -> None:
    """Both the chunk rows and the processing columns must survive unchanged."""
    assert state["chunk_count"] == _SEEDED_CHUNK_COUNT, f"[{cell}] chunk rows were discarded"
    assert state["pdf_downloaded"] is True, f"[{cell}] pdf_downloaded was reset"
    assert state["pdf_local_path"] == _SEEDED_PDF_PATH, f"[{cell}] pdf_local_path was reset"
    assert state["chunked_at"] is not None, f"[{cell}] chunked_at was reset"


# ---------------------------------------------------------------------------
# Layer probes
# ---------------------------------------------------------------------------


async def _layer_a_visible(conn, paper_id: int, user_id: int) -> bool:
    """Single-paper layer: True iff assert_paper_ownership grants access."""
    try:
        await assert_paper_ownership(conn, paper_id, user_id)
    except HTTPException as exc:
        assert exc.status_code == 403, f"unexpected status from layer A: {exc.status_code}"
        return False
    return True


async def _layer_b_visible(conn, paper_id: int, user_id: int) -> bool:
    """Return whether the paper survives the shipped bulk predicate."""
    membership_sql = f"""
        SELECT papers.id FROM papers
        WHERE papers.id = $1
          AND {paper_visible_sql(2, alias="papers")}
    """
    rows = await conn.fetch(membership_sql, paper_id, user_id)
    return len(rows) == 1


async def _assert_layers_agree(
    conn, paper_id: int, user_id: int, expected: bool, cell: str
) -> None:
    """Both layers must return *expected* for (paper_id, user_id)."""
    a = await _layer_a_visible(conn, paper_id, user_id)
    b = await _layer_b_visible(conn, paper_id, user_id)
    assert a == b, (
        f"[{cell}] layers disagree: assert_paper_ownership={a!r}, paper_visible_sql={b!r}"
    )
    assert a == expected, f"[{cell}] expected visible={expected!r}, both layers gave {a!r}"


# ---------------------------------------------------------------------------
# 4-cell agreement matrix
# ---------------------------------------------------------------------------


async def test_private_discoverer_without_membership_is_invisible(contract_conn):
    """Discoverer attribution cannot expose a private row."""
    caller = await seed_user_row(contract_conn, "vis-owner@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-owned", discovered_by=caller)
    await _assert_layers_agree(
        contract_conn, paper, caller, expected=False, cell="private-discoverer"
    )


async def test_public_paper_visible_independently_of_discoverer(contract_conn):
    """Persisted public scope is visible without a library row."""
    caller = await seed_user_row(contract_conn, "vis-shared@contract.example.com")
    other = await seed_user_row(contract_conn, "vis-shared-other@contract.example.com")
    paper = await _seed_paper(
        contract_conn,
        "vis-shared",
        visibility_scope="public",
        discovered_by=other,
    )
    await _assert_layers_agree(contract_conn, paper, caller, expected=True, cell="public")


async def test_private_paper_in_library_visible(contract_conn):
    """Explicit caller-library membership exposes a private row."""
    caller = await seed_user_row(contract_conn, "vis-lib-caller@contract.example.com")
    other = await seed_user_row(contract_conn, "vis-lib-other@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-other-in-lib", discovered_by=other)
    await _add_to_library(contract_conn, caller, paper)
    await _assert_layers_agree(
        contract_conn, paper, caller, expected=True, cell="other-owned-in-library"
    )


async def test_unattributed_private_paper_not_in_library_invisible(contract_conn):
    """Null audit attribution does not make a private row public."""
    caller = await seed_user_row(contract_conn, "vis-nolib-caller@contract.example.com")
    paper = await _seed_paper(contract_conn, "vis-other-not-in-lib")
    await _assert_layers_agree(
        contract_conn, paper, caller, expected=False, cell="private-unattributed"
    )


async def test_unknown_source_obeys_scope_and_library_only(contract_conn):
    """An unknown source stays private but remains usable when explicitly shelved."""
    caller = await seed_user_row(contract_conn, "vis-unknown@contract.example.com")
    paper = await _seed_paper(
        contract_conn,
        "vis-unknown",
        visibility_scope="private",
        source_type="future_adapter",
    )
    await _assert_layers_agree(contract_conn, paper, caller, expected=False, cell="unknown-private")
    await _add_to_library(contract_conn, caller, paper)
    await _assert_layers_agree(contract_conn, paper, caller, expected=True, cell="unknown-library")


async def test_upsert_paths_persist_private_default_and_trusted_public_promotion(
    contract_conn,
) -> None:
    """Real upserts enforce the request/trusted-adapter WRITE-authority boundary.

    The unverified client path (``upsert_paper``) inserts private and is
    attach-only on conflict: it mutates no canonical column of an existing
    shared row (neither content nor scope). The server-owned adapter path
    (``upsert_verified_public_paper``) inserts public and, on conflict, re-owns
    EVERY client-provided descriptive column and forces public scope, while
    preserving the insert-only audit provenance
    (``discovered_by``/``discovery_origin``). Because a promotion that replaces
    the row's source URL leaves its PDF-derived content describing the previous
    one, that content is discarded rather than published under the new scope.
    """
    from datetime import date

    from paper_ingestion.models.papers import PaperCreate, SourceType
    from paper_ingestion.services.pdf_workflow import (
        upsert_paper,
        upsert_verified_public_paper,
    )

    # --- New rows: client path is private, adapter path is public ---
    private_row = await upsert_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-private-default",
            source_type=SourceType.ARXIV,
            title="Private default",
            authors=["A. Author"],
            url="https://example.test/upsert-private-default",
        ),
    )
    assert private_row["visibility_scope"] == "private"
    assert private_row["is_insert"]

    # --- Attach-only: the client path never mutates an existing shared row ---
    owner = await upsert_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-attach-only",
            source_type=SourceType.ARXIV,
            title="Owner title",
            authors=["Owner Author"],
            abstract="owner abstract",
            published_date=date(2020, 1, 1),
            url="https://example.test/owner",
            pdf_url="https://example.test/owner.pdf",
            citation_count=3,
            metadata={"owner": True},
        ),
    )
    attached = await upsert_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-attach-only",
            source_type=SourceType.LOCAL,
            title="Client overwrite attempt",
            authors=["Attacker"],
            abstract="attacker abstract",
            published_date=date(2099, 12, 31),
            url="https://example.test/attacker",
            pdf_url="https://example.test/attacker.pdf",
            citation_count=999,
            metadata={"attacker": True},
        ),
    )
    assert attached["id"] == owner["id"]
    assert not attached["is_insert"]
    for column in (
        "source_type",
        "title",
        "authors",
        "abstract",
        "published_date",
        "url",
        "pdf_url",
        "citation_count",
        "metadata",
        "visibility_scope",
    ):
        assert attached[column] == owner[column], f"attach-only mutated {column}"
    assert attached["visibility_scope"] == "private"

    # --- trusted promotion re-owns EVERY client column + forces public ---
    audit_user = await seed_user_row(contract_conn, "ten2b-discoverer@contract.example.com")
    seeded = await upsert_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-trusted-refresh",
            source_type=SourceType.LOCAL,
            title="Attacker title",
            authors=["Attacker"],
            abstract="attacker abstract",
            published_date=date(2099, 12, 31),
            url="https://example.test/attacker-seed",
            pdf_url="https://example.test/attacker-seed.pdf",
            citation_count=999,
            metadata={"seed": "attacker"},
        ),
        discovered_by=audit_user,
    )
    assert seeded["visibility_scope"] == "private"
    assert seeded["discovery_origin"] == "user_initiated"
    # Content produced from the seeded row's own pdf_url, as a completed PDF run
    # would leave it behind.
    await _seed_derived_pdf_content(contract_conn, seeded["id"])

    promoted = await upsert_verified_public_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-trusted-refresh",
            source_type=SourceType.ARXIV,
            title="Verified title",
            authors=["Verified Author"],
            abstract="verified abstract",
            published_date=date(2021, 6, 1),
            url="https://arxiv.org/abs/verified",
            pdf_url="https://arxiv.org/pdf/verified.pdf",
            citation_count=7,
            metadata={"source": "verified"},
            discovery_origin="pulse",
        ),
        discovered_by=None,
    )
    assert promoted["id"] == seeded["id"]
    assert promoted["visibility_scope"] == "public"
    assert promoted["source_type"] == "arxiv"
    assert promoted["title"] == "Verified title"
    assert promoted["authors"] == ["Verified Author"]
    assert promoted["abstract"] == "verified abstract"
    assert promoted["published_date"] == date(2021, 6, 1)
    assert promoted["url"] == "https://arxiv.org/abs/verified"
    assert promoted["pdf_url"] == "https://arxiv.org/pdf/verified.pdf"
    assert promoted["citation_count"] == 7
    assert promoted["metadata"] == {"source": "verified"}
    # Insert-only provenance is preserved, NOT overwritten by the promotion.
    assert promoted["discovered_by"] == audit_user
    assert promoted["discovery_origin"] == "user_initiated"

    # The promotion supplied a different pdf_url, so everything derived from the
    # superseded one is discarded instead of being published under public scope.
    state = await _derived_pdf_content(contract_conn, promoted["id"])
    assert state["chunk_count"] == 0, "chunk rows from the superseded pdf_url must be deleted"
    assert state["pdf_downloaded"] is False
    assert state["pdf_local_path"] is None
    assert state["chunked_at"] is None
    # Callers echo the returned record straight into their response, so it must
    # report the reset rather than the pre-promotion pointers.
    assert promoted["pdf_downloaded"] is False, (
        "returned record must not echo a stale download flag"
    )
    assert promoted["pdf_local_path"] is None, "returned record must not echo a stale local path"
    assert promoted["chunked_at"] is None
    assert promoted["is_insert"] is False

    # The client path also cannot demote or mutate the now-public shared row.
    client_replay = await upsert_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-trusted-refresh",
            source_type=SourceType.LOCAL,
            title="Demotion attempt",
            authors=["Attacker"],
            url="https://example.test/demote",
        ),
    )
    assert client_replay["id"] == promoted["id"]
    assert client_replay["visibility_scope"] == "public"
    assert client_replay["source_type"] == "arxiv"
    assert client_replay["title"] == "Verified title"


# ---------------------------------------------------------------------------
# Derived-content retention: the three conditions under which a promotion must
# leave PDF-derived content alone. Each test pins exactly one of them.
# ---------------------------------------------------------------------------

_ADAPTER_PDF_URL = "https://arxiv.org/pdf/2401.00002.pdf"


def _adapter_paper(external_id: str, pdf_url: str | None):
    """Build the metadata a verified arXiv adapter returns for *external_id*."""
    from paper_ingestion.models.papers import PaperCreate, SourceType

    return PaperCreate(
        external_id=external_id,
        source_type=SourceType.ARXIV,
        title="Verified title",
        authors=["Verified Author"],
        url="https://arxiv.org/abs/derived-content",
        pdf_url=pdf_url,
    )


async def test_promotion_retains_derived_content_when_source_url_is_unchanged(
    contract_conn,
) -> None:
    """A promotion that confirms the stored source URL leaves derived content in place."""
    from paper_ingestion.services.pdf_workflow import (
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn, _adapter_paper("promotion-same-url", _ADAPTER_PDF_URL)
    )
    await _seed_derived_pdf_content(contract_conn, seeded["id"])

    promoted = await upsert_verified_public_paper(
        contract_conn, _adapter_paper("promotion-same-url", _ADAPTER_PDF_URL)
    )

    assert promoted["id"] == seeded["id"]
    assert promoted["visibility_scope"] == "public"
    assert promoted["pdf_downloaded"] is True, "returned record must keep the download state"
    _assert_derived_pdf_content_intact(
        await _derived_pdf_content(contract_conn, promoted["id"]), "same-url"
    )


async def test_public_paper_discards_derived_content_when_source_url_changes(
    contract_conn,
) -> None:
    """A source revision invalidates content even when the row is already public."""
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    created = await upsert_verified_public_paper(
        contract_conn, _adapter_paper("promotion-public-revision", _ADAPTER_PDF_URL)
    )
    assert created["visibility_scope"] == "public"
    await _seed_derived_pdf_content(contract_conn, created["id"])

    refreshed = await upsert_verified_public_paper(
        contract_conn,
        _adapter_paper("promotion-public-revision", "https://arxiv.org/pdf/2401.00002v2.pdf"),
    )

    assert refreshed["id"] == created["id"]
    assert refreshed["pdf_url"] == "https://arxiv.org/pdf/2401.00002v2.pdf"
    state = await _derived_pdf_content(contract_conn, refreshed["id"])
    assert state["chunk_count"] == 0
    assert state["pdf_downloaded"] is False
    assert state["pdf_local_path"] is None
    assert state["chunked_at"] is None
    assert refreshed["pdf_downloaded"] is False
    assert refreshed["pdf_local_path"] is None
    assert refreshed["chunked_at"] is None


async def test_first_public_insert_reports_insert_and_discards_nothing(contract_conn) -> None:
    """A brand-new public row takes no discard path, so callers still see is_insert."""
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    inserted = await upsert_verified_public_paper(
        contract_conn, _adapter_paper("promotion-first-insert", _ADAPTER_PDF_URL)
    )

    assert inserted["is_insert"] is True, "an insert must not be reported as a conflict"
    assert inserted["visibility_scope"] == "public"
    assert inserted["pdf_downloaded"] is False
    assert inserted["pdf_local_path"] is None


async def test_promotion_discards_derived_content_inside_nested_transactions(
    contract_conn,
) -> None:
    """The discard behaves correctly at the transaction depth the deck pipeline uses.

    The deck pipeline promotes each card inside a deck-wide transaction plus a
    per-card savepoint, and the promotion opens a block of its own inside those.
    asyncpg maps nested blocks onto SAVEPOINTs, so this runs the real promotion
    at that depth and pins that the discard commits and the enclosing blocks
    stay usable afterwards.
    """
    from paper_ingestion.services.pdf_workflow import (
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn,
        _adapter_paper("promotion-nested", "https://arxiv.org/pdf/2401.00003.pdf"),
    )
    await _seed_derived_pdf_content(contract_conn, seeded["id"])

    async with contract_conn.transaction():
        async with contract_conn.transaction():
            promoted = await upsert_verified_public_paper(
                contract_conn, _adapter_paper("promotion-nested", _ADAPTER_PDF_URL)
            )
        enclosing_block_usable = await contract_conn.fetchval("SELECT 1")

    assert enclosing_block_usable == 1, "the deck-wide block must survive the promotion"
    assert promoted["id"] == seeded["id"]
    assert promoted["pdf_downloaded"] is False
    state = await _derived_pdf_content(contract_conn, promoted["id"])
    assert state["chunk_count"] == 0
    assert state["pdf_downloaded"] is False
    assert state["chunked_at"] is None


# ---------------------------------------------------------------------------
# Storage reclamation after a discard.
#
# Reclaiming the vector points, the stored PDF and the page images is disk
# housekeeping, not the control that stops the superseded text being served —
# that is the stored-chunk requirement in rag/streaming.py. These tests pin both
# halves: the promotion write path does reclaim, and a reclamation that fails
# changes neither the committed promotion nor what retrieval returns.
# ---------------------------------------------------------------------------

_SUPERSEDED_EXCERPT = "Text extracted from the superseded source URL."


def _redirect_storage_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paper_id: int
) -> tuple[Path, Path]:
    """Point both storage roots at *tmp_path* and fill them for *paper_id*.

    Returns the stored PDF path and the page-image directory a completed run
    would have left behind.
    """
    from paper_ingestion.services import pdf_workflow

    pdf_root = tmp_path / "pdfs"
    snapshot_root = tmp_path / "snapshots"
    pdf_root.mkdir()
    pdf_path = pdf_root / f"{paper_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 superseded")
    snapshot_dir = snapshot_root / str(paper_id)
    snapshot_dir.mkdir(parents=True)
    for page in (1, 2):
        (snapshot_dir / f"page_{page}.png").write_bytes(b"")
    monkeypatch.setattr(pdf_workflow, "PDF_STORAGE_PATH", str(pdf_root))
    monkeypatch.setattr(pdf_workflow, "SNAPSHOT_STORAGE_PATH", str(snapshot_root))
    return pdf_path, snapshot_dir


async def test_search_persistence_reports_a_middle_failure_and_continues(
    contract_conn, monkeypatch
) -> None:
    """A bad result neither hides earlier saves nor prevents later saves and cleanup."""
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.routers import search as search_router
    from paper_ingestion.services.pdf_workflow import upsert_paper

    caller = await seed_user_row(contract_conn, "partial-search@contract.example.com")
    external_ids = [
        "partial-search-first",
        "partial-search-middle",
        "partial-search-last",
    ]
    first = await upsert_paper(
        contract_conn,
        _adapter_paper(external_ids[0], "https://arxiv.org/pdf/2401.20001.pdf"),
    )
    last = await upsert_paper(
        contract_conn,
        _adapter_paper(external_ids[2], "https://arxiv.org/pdf/2401.20003.pdf"),
    )
    await _seed_derived_pdf_content(contract_conn, first["id"])
    await _seed_derived_pdf_content(contract_conn, last["id"])

    real_upsert = search_router.upsert_verified_public_paper

    async def _fail_middle(conn, paper, **kwargs):
        if paper.external_id == external_ids[1]:
            raise RuntimeError("simulated persistence failure")
        return await real_upsert(conn, paper, **kwargs)

    reclaimed_ids: list[int] = []

    async def _record_reclamation(paper_id: int, _db_pool) -> None:
        reclaimed_ids.append(paper_id)

    monkeypatch.setattr(search_router, "upsert_verified_public_paper", _fail_middle)
    monkeypatch.setattr(
        search_router,
        "reclaim_discarded_paper_content",
        _record_reclamation,
    )

    saved, failed = await search_router._persist_search_results(
        SharedConnPool(contract_conn),
        [_adapter_paper(external_id, _ADAPTER_PDF_URL) for external_id in external_ids],
        caller,
    )

    assert [paper.external_id for paper in saved] == [external_ids[0], external_ids[2]]
    assert [(failure.external_id, failure.error) for failure in failed] == [
        (external_ids[1], "unknown_error")
    ]
    assert reclaimed_ids == [first["id"], last["id"]]

    persisted = await contract_conn.fetch(
        """SELECT p.external_id
             FROM papers p
             JOIN user_library ul ON ul.paper_id = p.id
            WHERE ul.user_id = $1 AND p.external_id = ANY($2::text[])
            ORDER BY p.external_id""",
        caller,
        external_ids,
    )
    assert [row["external_id"] for row in persisted] == sorted([external_ids[0], external_ids[2]])


async def test_search_promotion_reclaims_the_discarded_files_and_vectors(
    contract_conn, monkeypatch, tmp_path
) -> None:
    """The search write path frees the storage a discarded promotion leaves behind."""
    # Verified: services/paper_ingestion/paper_ingestion/routers/search.py:206
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.routers.search import _persist_search_results
    from paper_ingestion.services import pdf_workflow
    from paper_ingestion.services.pdf_workflow import upsert_paper

    caller = await seed_user_row(contract_conn, "reclaim-search@contract.example.com")
    seeded = await upsert_paper(
        contract_conn,
        _adapter_paper("promotion-reclaim-search", "https://arxiv.org/pdf/2401.10001.pdf"),
    )
    await _seed_derived_pdf_content(contract_conn, seeded["id"])
    pdf_path, snapshot_dir = _redirect_storage_roots(tmp_path, monkeypatch, seeded["id"])

    reclaimed_vector_ids: list[int] = []

    async def _record_vector_delete(paper_id: int) -> None:
        reclaimed_vector_ids.append(paper_id)

    monkeypatch.setattr(pdf_workflow, "delete_paper_vectors", _record_vector_delete)

    saved, failed = await _persist_search_results(
        SharedConnPool(contract_conn),
        [_adapter_paper("promotion-reclaim-search", _ADAPTER_PDF_URL)],
        caller,
    )

    assert len(saved) == 1
    assert failed == []
    assert (await _derived_pdf_content(contract_conn, seeded["id"]))["chunk_count"] == 0, (
        "precondition: the promotion discarded the derived content"
    )
    assert reclaimed_vector_ids == [seeded["id"]], (
        f"the vector delete must run exactly once for the promoted paper: {reclaimed_vector_ids}"
    )
    assert not pdf_path.exists(), "the PDF stored for the superseded source URL must be removed"
    assert not snapshot_dir.exists(), (
        "page images rendered from the superseded PDF must be removed whole, so a shorter "
        "re-derived document cannot leave higher-numbered pages behind"
    )


async def test_promotion_stands_when_reclamation_fails_and_retrieval_stays_closed(
    contract_conn, monkeypatch, tmp_path
) -> None:
    """A failing reclamation is absorbed rather than rolled back.

    Reclamation is housekeeping, so its failure must not undo a committed
    promotion and must not reach the caller: the route awaiting this write path
    has no handler for it and would answer 500 instead of 200. What a library
    member gets back for the superseded text is decided by the stored-chunk
    requirement, which the failure leaves untouched.
    """
    # Verified: services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:1171
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagNoResults, prepare_cross_paper_rag
    from paper_ingestion.routers.search import _persist_search_results
    from paper_ingestion.services import pdf_workflow
    from paper_ingestion.services.pdf_workflow import upsert_paper

    reader = await seed_user_row(contract_conn, "reclaim-fault@contract.example.com")
    seeded = await upsert_paper(
        contract_conn,
        _adapter_paper("promotion-reclaim-fault", "https://arxiv.org/pdf/2401.10002.pdf"),
    )
    await _seed_derived_pdf_content(contract_conn, seeded["id"])
    pdf_path, snapshot_dir = _redirect_storage_roots(tmp_path, monkeypatch, seeded["id"])

    async def _fail_vector_delete(paper_id: int) -> None:
        raise RuntimeError("vector store unavailable")

    monkeypatch.setattr(pdf_workflow, "delete_paper_vectors", _fail_vector_delete)

    pool = SharedConnPool(contract_conn)
    saved, failed = await _persist_search_results(
        pool,
        [_adapter_paper("promotion-reclaim-fault", _ADAPTER_PDF_URL)],
        reader,
    )

    assert len(saved) == 1, "the write path must complete so its route can still answer"
    assert failed == []
    state = await _derived_pdf_content(contract_conn, seeded["id"])
    assert state["chunk_count"] == 0, "the promotion must stay committed"
    assert state["pdf_downloaded"] is False
    assert state["pdf_local_path"] is None
    # The steps after the failing one still run.
    assert not pdf_path.exists()
    assert not snapshot_dir.exists()

    # The vector layer still offers the superseded excerpt, exactly the state a
    # failed vector delete leaves behind.
    embedder = SimpleNamespace(
        search_chunks_global=AsyncMock(
            return_value=[
                {
                    "paper_id": seeded["id"],
                    "chunk_index": 0,
                    "content": _SUPERSEDED_EXCERPT,
                    "page_number": 1,
                    "score": 0.95,
                }
            ]
        ),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )
    result = await prepare_cross_paper_rag(
        embedder,
        pool,
        CrossPaperAskRequest(question="What does the paper say?", decompose=False),
        AsyncMock(),
        user_id=reader,
    )

    assert isinstance(result, CrossPaperRagNoResults), (
        f"retrieval must yield nothing for the superseded text, got {result!r}"
    )
    assert result.sources == []


# ---------------------------------------------------------------------------
# The promotion boundary end to end, over HTTP.
#
# POST /api/search is the route a reader reaches a promotion through, so these
# two run the whole path rather than the write helper underneath it: a private
# paper carrying the derived content of a completed run, a promotion made over
# HTTP by a second user, then that user's own reads of the paper detail, the
# stored PDF, a page image and cross-paper retrieval. Only the source plugin
# and the vector store are stubbed. The pair differs in one input — whether the
# adapter confirms the stored source URL or replaces it — so together they pin
# both that a superseded read is closed and that a confirmed one is untouched.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _promotion_app(contract_conn):
    """Paper-ingestion app on the contract connection, with session-cookie auth active."""
    from unittest.mock import MagicMock

    from jarvis_common import (
        current_user_id_strict_with_owner_override,
        get_current_user_id,
    )
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides
    from paper_ingestion.main import app

    with (
        patch_app_state(
            app,
            {
                "db_pool": SharedConnPool(contract_conn),
                "embedder": None,
                "http_client": MagicMock(),
            },
        ),
        patch_dependency_overrides(
            app,
            remove_overrides={
                current_user_id_strict_with_owner_override,
                get_current_user_id,
            },
        ),
    ):
        yield app


def _serve_storage_roots_to_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the PDF and page-image routes at the roots the workflow was redirected to."""
    from paper_ingestion.routers import pdfs, snapshots

    monkeypatch.setattr(pdfs, "PDF_STORAGE_PATH", str(tmp_path / "pdfs"))
    monkeypatch.setattr(snapshots, "SNAPSHOT_STORAGE_PATH", str(tmp_path / "snapshots"))


def _adapter_returns(monkeypatch: pytest.MonkeyPatch, paper) -> None:
    """Stub source resolution so POST /api/search yields *paper* from the arXiv adapter."""
    from unittest.mock import AsyncMock

    from paper_ingestion.models import SourceType

    plugin = AsyncMock()
    plugin.search = AsyncMock(return_value=[paper])

    async def _resolver(source_types, db_pool, http_client, request):
        return {SourceType.ARXIV: plugin}, {}

    monkeypatch.setattr("paper_ingestion.routers.search._resolve_sources_for_search", _resolver)


_SEARCH_BODY = {"query": "verified title", "source_types": ["arxiv"], "max_results": 5}


async def test_promotion_over_http_closes_every_read_of_the_superseded_content(
    contract_conn, contract_two_users, _promotion_app, _configure_api_key, monkeypatch, tmp_path
) -> None:
    """Promoting over POST /api/search leaves the promoting reader nothing derived to read.

    The saving user's paper carries a source URL of its own plus everything a
    completed run produces: chunk rows, a stored PDF and rendered page images.
    The second user's search returns the adapter's own URL for the same
    external id, so the promotion supersedes all of it. Every surface that
    could still hand that content back is then read as that second user.

    The file routes are read twice. Reclamation has already removed both files
    by the time the promotion route returns, so the first pair of 404s does not
    on its own show the stored-pointer predicate doing anything. The second pair
    is taken with the files restored on disk, where only that predicate can
    still refuse them.
    """
    # Verified: services/paper_ingestion/paper_ingestion/routers/search.py:250 (POST /api/search)
    # Verified: services/paper_ingestion/paper_ingestion/routers/papers_detail.py:67 (chunks)
    # Verified: services/paper_ingestion/paper_ingestion/routers/pdfs.py:70 (GET /api/pdfs)
    # Verified: services/paper_ingestion/paper_ingestion/routers/snapshots.py:18 (page images)
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import make_contract_client
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagNoResults, prepare_cross_paper_rag
    from paper_ingestion.services import pdf_workflow
    from paper_ingestion.services.pdf_workflow import upsert_paper

    external_id = "promotion-http-superseded"
    seeded = await upsert_paper(
        contract_conn, _adapter_paper(external_id, "https://arxiv.org/pdf/2401.30001.pdf")
    )
    paper_id = int(seeded["id"])
    await _add_to_library(contract_conn, contract_two_users.user_a_id, paper_id)
    await _seed_derived_pdf_content(contract_conn, paper_id)
    pdf_path, snapshot_dir = _redirect_storage_roots(tmp_path, monkeypatch, paper_id)
    _serve_storage_roots_to_routes(monkeypatch, tmp_path)
    monkeypatch.setattr(pdf_workflow, "delete_paper_vectors", AsyncMock())
    _adapter_returns(monkeypatch, _adapter_paper(external_id, _ADAPTER_PDF_URL))

    async with make_contract_client(_promotion_app, contract_two_users.cookie_a) as owner:
        owner_pdf = await owner.get(f"/api/pdfs/{paper_id}")
        owner_page = await owner.get(f"/api/snapshots/{paper_id}/1")
    assert owner_pdf.status_code == 200, (
        "precondition: the files derived from the saving user's own source URL do serve"
    )
    assert owner_page.status_code == 200, "precondition: the rendered page image does serve"

    async with make_contract_client(_promotion_app, contract_two_users.cookie_b) as reader:
        promotion = await reader.post("/api/search", json=_SEARCH_BODY)
        assert promotion.status_code == 200, promotion.text[:300]
        persistence = promotion.json()
        assert [paper["external_id"] for paper in persistence["saved"]] == [external_id]
        assert persistence["failed"] == []
        detail = await reader.get(f"/api/papers/{paper_id}")
        pdf = await reader.get(f"/api/pdfs/{paper_id}")
        page = await reader.get(f"/api/snapshots/{paper_id}/1")

    assert detail.status_code == 200, detail.text[:300]
    assert detail.json()["chunks"] == [], (
        f"the paper must report no stored chunk: {detail.json()['chunks']}"
    )
    assert pdf.status_code == 404, f"the superseded PDF must not serve: {pdf.status_code}"
    assert page.status_code == 404, f"the superseded page image must not serve: {page.status_code}"
    assert not pdf_path.exists(), "the deferred reclamation must remove the stored PDF"
    assert not snapshot_dir.exists(), "the deferred reclamation must remove the page images"

    # The promotion route awaits reclamation before returning, so both files
    # were already gone when those two requests ran and either route would have
    # answered 404 whatever the stored-pointer predicate said. Putting the files
    # back and asking again separates the two: the paper is still visible to
    # this reader and the bytes are on disk, so only the pointer the promotion
    # cleared can still refuse them.
    pdf_path.write_bytes(b"%PDF-1.4 restored on disk")
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "page_1.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    async with make_contract_client(_promotion_app, contract_two_users.cookie_b) as reader:
        restored_pdf = await reader.get(f"/api/pdfs/{paper_id}")
        restored_page = await reader.get(f"/api/snapshots/{paper_id}/1")

    assert restored_pdf.status_code == 404, (
        "with the file back on disk the refusal must come from the cleared pointer, "
        f"got {restored_pdf.status_code}"
    )
    assert restored_page.status_code == 404, (
        "the page-image route shares that guard, so it must refuse for the same reason, "
        f"got {restored_page.status_code}"
    )

    # Retrieval is asked with the vector layer still offering the superseded
    # excerpt — the state a failed or deferred vector delete leaves behind.
    embedder = SimpleNamespace(
        search_chunks_global=AsyncMock(
            return_value=[
                {
                    "paper_id": paper_id,
                    "chunk_index": 0,
                    "content": _SUPERSEDED_EXCERPT,
                    "page_number": 1,
                    "score": 0.95,
                }
            ]
        ),
        rerank_chunks=AsyncMock(side_effect=lambda _q, candidates, top_k: candidates[:top_k]),
    )
    result = await prepare_cross_paper_rag(
        embedder,
        SharedConnPool(contract_conn),
        CrossPaperAskRequest(question="What does the paper say?", decompose=False),
        AsyncMock(),
        user_id=contract_two_users.user_b_id,
    )
    assert isinstance(result, CrossPaperRagNoResults), (
        f"retrieval must yield nothing for the superseded text, got {result!r}"
    )
    assert result.sources == []


async def test_promotion_confirming_the_source_url_keeps_the_paper_whole_over_http(
    contract_conn, contract_two_users, _promotion_app, _configure_api_key, monkeypatch, tmp_path
) -> None:
    """A promotion that confirms the stored source URL changes nothing a reader can reach.

    Same route and same reader as the superseded case; the one difference is
    that the adapter returns the URL the paper already carries. The derived
    columns and chunk rows must survive, the stored PDF must still serve over
    HTTP, and nothing may decide the paper needs analysing again. The reading
    list is where that decision is made, so it is exercised here: it reads
    exactly the chunk rows the promotion left in place.
    """
    # Verified: services/paper_ingestion/paper_ingestion/routers/pdfs.py:70 (GET /api/pdfs)
    # Verified: services/paper_ingestion/paper_ingestion/routers/papers_lifecycle.py:48
    #   (PUT /api/papers/{id}/save defers paper.analyze only when no chunk row exists)
    import jarvis_common.task_registry as task_registry
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.testing_contract_apps import make_contract_client
    from paper_ingestion.services import pdf_workflow
    from paper_ingestion.services.pdf_workflow import upsert_paper

    external_id = "promotion-http-confirmed"
    seeded = await upsert_paper(contract_conn, _adapter_paper(external_id, _ADAPTER_PDF_URL))
    paper_id = int(seeded["id"])
    await _add_to_library(contract_conn, contract_two_users.user_a_id, paper_id)
    await _seed_derived_pdf_content(contract_conn, paper_id)
    pdf_path, snapshot_dir = _redirect_storage_roots(tmp_path, monkeypatch, paper_id)
    _serve_storage_roots_to_routes(monkeypatch, tmp_path)
    reclaimed_vector_ids: list[int] = []

    async def _record_vector_delete(reclaimed_id: int) -> None:
        reclaimed_vector_ids.append(reclaimed_id)

    monkeypatch.setattr(pdf_workflow, "delete_paper_vectors", _record_vector_delete)
    _adapter_returns(monkeypatch, _adapter_paper(external_id, _ADAPTER_PDF_URL))

    analyze_task = MagicMock()
    analyze_task.defer_async = AsyncMock()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": analyze_task}):
        async with make_contract_client(_promotion_app, contract_two_users.cookie_b) as reader:
            promotion = await reader.post("/api/search", json=_SEARCH_BODY)
            assert promotion.status_code == 200, promotion.text[:300]
            pdf = await reader.get(f"/api/pdfs/{paper_id}")
            reading_list = await reader.put(f"/api/papers/{paper_id}/save")

    assert pdf.status_code == 200, f"the stored PDF must still serve: {pdf.status_code}"
    assert reading_list.status_code == 200, reading_list.text[:300]
    analyze_task.defer_async.assert_not_awaited()
    assert reclaimed_vector_ids == [], (
        f"nothing was discarded, so no vector delete may run: {reclaimed_vector_ids}"
    )
    _assert_derived_pdf_content_intact(
        await _derived_pdf_content(contract_conn, paper_id), "http-confirmed"
    )
    assert pdf_path.exists() and snapshot_dir.exists(), (
        "a confirmed source URL must leave the stored files in place"
    )


# ---------------------------------------------------------------------------
# Auto-summary holder selection (auto_fetch._UNSUMMARIZED_HOLDERS_SQL)
#
# Discovery defers one paper.summarize per library holder that lacks a summary
# OF THEIR OWN. Summaries are per-user by schema and every reader binds a strict
# integer owner, so the selection's correlated NOT EXISTS must key on BOTH
# paper_id and user_id. A paper-global check (the shipped bug this replaces)
# silently starves every holder after the first. Mocks cannot tell a correct
# correlation from a subtly wrong one, so these run the shipped SQL constant
# against real Postgres.
#
# Verified: services/paper_ingestion/paper_ingestion/pipelines/auto_fetch.py
#           (_UNSUMMARIZED_HOLDERS_SQL — imported here, never re-typed)
# ---------------------------------------------------------------------------


async def _seed_summary(conn, paper_id: int, user_id: int) -> None:
    """Give *user_id* their own summary row for *paper_id*."""
    await conn.execute(
        """INSERT INTO paper_summaries (paper_id, user_id, summary_brief, summary_detailed)
           VALUES ($1, $2, 'brief', 'detailed')""",
        paper_id,
        user_id,
    )


async def _unsummarized_holders(conn, paper_id: int) -> set[int]:
    """Run the shipped holder-selection query; return the selected user ids."""
    from paper_ingestion.pipelines.auto_fetch import _UNSUMMARIZED_HOLDERS_SQL

    rows = await conn.fetch(_UNSUMMARIZED_HOLDERS_SQL, paper_id)
    return {int(row["user_id"]) for row in rows}


async def test_holder_without_any_summary_is_selected(contract_conn):
    """A library holder with no summary at all is selected for summarization."""
    holder = await seed_user_row(contract_conn, "sum-plain-holder@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-plain")
    await _add_to_library(contract_conn, holder, paper)

    assert await _unsummarized_holders(contract_conn, paper) == {holder}


async def test_holder_with_own_summary_is_not_selected(contract_conn):
    """A holder who already has THEIR OWN summary is skipped — no redundant LLM spend."""
    holder = await seed_user_row(contract_conn, "sum-own-holder@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-own")
    await _add_to_library(contract_conn, holder, paper)
    await _seed_summary(contract_conn, paper, holder)

    assert await _unsummarized_holders(contract_conn, paper) == set()


async def test_holder_with_only_a_stale_summary_is_selected(contract_conn):
    """A prior-generation summary cannot suppress automatic regeneration."""
    holder = await seed_user_row(contract_conn, "sum-stale-holder@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-stale")
    await _add_to_library(contract_conn, holder, paper)
    await _seed_summary(contract_conn, paper, holder)
    await contract_conn.execute(
        "UPDATE papers SET content_generation = 1 WHERE id = $1",
        paper,
    )

    assert await _unsummarized_holders(contract_conn, paper) == {holder}


async def test_holder_still_selected_when_a_different_user_has_a_summary(contract_conn):
    """The regression case: user A's summary must NOT suppress holder B.

    A paper-global EXISTS(paper_summaries WHERE paper_id = $1) — the shipped bug —
    returns zero holders here, leaving B with a summary they can never read.
    """
    summarized = await seed_user_row(contract_conn, "sum-cross-a@contract.example.com")
    pending = await seed_user_row(contract_conn, "sum-cross-b@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-cross")
    await _add_to_library(contract_conn, summarized, paper)
    await _add_to_library(contract_conn, pending, paper)
    await _seed_summary(contract_conn, paper, summarized)

    selected = await _unsummarized_holders(contract_conn, paper)
    assert pending in selected, (
        "holder B has no summary of their own and MUST still be selected; "
        "a paper-global summary check would starve them"
    )
    assert summarized not in selected


async def test_non_holder_is_never_selected_even_with_a_summary_row(contract_conn):
    """Selection is driven by user_library membership, never by paper_summaries.

    A user with a summary row but no library entry must not be re-summarized.
    """
    holder = await seed_user_row(contract_conn, "sum-nonholder-holder@contract.example.com")
    stranger = await seed_user_row(contract_conn, "sum-nonholder-stranger@contract.example.com")
    paper = await _seed_paper(contract_conn, "sum-nonholder")
    await _add_to_library(contract_conn, holder, paper)
    await _seed_summary(contract_conn, paper, stranger)

    assert await _unsummarized_holders(contract_conn, paper) == {holder}


async def test_process_library_marks_a_stale_summary_for_regeneration(contract_conn):
    """Process-library treats a retained prior-generation summary as missing."""
    from paper_ingestion.paper_jobs import _PROCESS_LIBRARY_SELECTION

    holder = await seed_user_row(contract_conn, "process-stale-holder@contract.example.com")
    paper = await _seed_paper(contract_conn, "process-stale")
    await _add_to_library(contract_conn, holder, paper)
    await _seed_summary(contract_conn, paper, holder)
    await contract_conn.execute(
        "UPDATE papers SET content_generation = 1 WHERE id = $1",
        paper,
    )

    rows = await contract_conn.fetch(
        _PROCESS_LIBRARY_SELECTION,
        holder,
        True,
        100,
        0,
    )
    selected = next(row for row in rows if int(row["id"]) == paper)
    assert selected["needs_summary"] is True


# ---------------------------------------------------------------------------
# Live Qdrant agreement and reconciliation gate
# ---------------------------------------------------------------------------


_LIVE_VECTOR = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class _LiveVisibilityEmbedder(EmbeddingStoreMixin, EmbeddingSearchMixin):
    """Compose the production storage/search mixins with deterministic vectors."""

    def __init__(self, qdrant: Any, generation: str) -> None:
        self.qdrant = qdrant
        self._live_generation = generation

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one fixed local vector per text without an external model."""
        return [list(_LIVE_VECTOR) for _ in texts]

    async def current_visibility_generation(self) -> str:
        """Return the generation selected by the live-test checkpoint."""
        return self._live_generation

    def set_generation(self, generation: str) -> None:
        """Advance the deterministic provider after a checkpoint rotation."""
        self._live_generation = generation


def _bind_live_collection(monkeypatch: pytest.MonkeyPatch, collection_name: str) -> None:
    """Route production mixins and reconciliation helpers to one isolated collection."""
    from paper_ingestion.ingestion import embed_store, embedding_config, payload_schema, search
    from paper_ingestion.services import pdf_workflow

    for module in (embed_store, embedding_config, payload_schema, search, pdf_workflow):
        monkeypatch.setattr(module, "COLLECTION_NAME", collection_name)


async def _seed_visibility_chunk(conn, paper_id: int, content: str) -> str:
    """Persist one chunk with the deterministic production point identity."""
    from paper_ingestion.ingestion.embed_store import chunk_point_id
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_MODEL_NAME

    point_id = chunk_point_id(paper_id, 0)
    await conn.execute(
        """INSERT INTO paper_chunks
                  (paper_id, chunk_index, content, page_number, start_char, end_char,
                   embedding_id, embedding_model)
           VALUES ($1, 0, $2, 1, 0, $3, $4, $5)""",
        paper_id,
        content,
        len(content),
        point_id,
        EMBEDDING_MODEL_NAME,
    )
    return point_id


async def _upsert_visibility_point(
    qdrant: Any,
    collection_name: str,
    *,
    paper_id: int,
    content: str,
    source_type: str,
    visibility_scope: str,
    visibility_generation: str,
    legacy_user_id: int | None = None,
    fingerprint_content: str | None = None,
) -> str:
    """Write one deterministic point, optionally simulating identity drift."""
    from paper_ingestion.ingestion.embed_store import (
        chunk_embedding_fingerprint,
        chunk_point_id,
    )
    from paper_ingestion.ingestion.embedding_config import EMBEDDING_MODEL_NAME
    from qdrant_client.models import PointStruct

    point_id = chunk_point_id(paper_id, 0)
    payload = {
        "paper_id": paper_id,
        "chunk_index": 0,
        "page_number": 1,
        "content": content,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "embedding_fingerprint": chunk_embedding_fingerprint(
            fingerprint_content if fingerprint_content is not None else content
        ),
        "source_type": source_type,
        "visibility_scope": visibility_scope,
        "visibility_generation": visibility_generation,
        "user_id": legacy_user_id,
    }
    await qdrant.upsert(
        collection_name=collection_name,
        points=[PointStruct(id=point_id, vector=_LIVE_VECTOR, payload=payload)],
        wait=True,
    )
    return point_id


async def _relationally_visible_ids(conn, paper_ids: Sequence[int], user_id: int) -> set[int]:
    """Recheck Qdrant candidates through the shipped relational authority."""
    if not paper_ids:
        return set()
    rows = await conn.fetch(
        f"SELECT p.id FROM papers p WHERE p.id = ANY($1::int[]) AND {paper_visible_sql(2)}",
        list(paper_ids),
        user_id,
    )
    return {int(row["id"]) for row in rows}


async def _feed_visible_ids(conn, paper_ids: set[int], user_id: int) -> set[int]:
    """Execute the production corpus-feed query and retain this test's rows."""
    from paper_ingestion.services.feed_query import build_feed_queries

    query = build_feed_queries(
        unread_only=False,
        sort="published_date",
        limit=200,
        offset=0,
        q=None,
        statuses=None,
        source_types=None,
        topic_names=None,
        date_from=None,
        date_to=None,
        user_id=user_id,
        scope="corpus",
    )
    rows = await conn.fetch(query.data_query, *query.params)
    return {int(row["id"]) for row in rows} & paper_ids


@pytest.mark.live_qdrant
async def test_live_qdrant_visibility_and_reconciliation_agree(
    contract_conn,
    monkeypatch: pytest.MonkeyPatch,
):
    """Prove release-critical visibility behavior against real Postgres and Qdrant.

    The matrix covers persisted public and private scope, explicit library
    membership, audit/source forgery, unknown sources, stale generations,
    payload-only repair, content-identity repair, explicit/cross/single-paper
    retrieval, copied-checkpoint rotation, and stale-worker fencing.
    """
    qdrant_url = os.environ.get("JARVIS_TEST_QDRANT_URL")
    collection_name = os.environ.get("JARVIS_TEST_QDRANT_COLLECTION")
    if not qdrant_url or not collection_name:
        pytest.fail(
            "live Qdrant gate requires JARVIS_TEST_QDRANT_URL and JARVIS_TEST_QDRANT_COLLECTION"
        )
    if not collection_name.startswith("jarvis-qdrant-"):
        pytest.fail("live Qdrant collection must use the generated jarvis-qdrant-* namespace")

    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.ingestion.payload_schema import (
        StaleVisibilityLeaseError,
        claim_visibility_lease,
        complete_visibility_checkpoint,
        ensure_visibility_payload_indexes,
        load_visibility_checkpoint,
        rotate_visibility_checkpoint,
        validate_checkpoint_collection_pair,
    )
    from paper_ingestion.ingestion.search_scope import SearchScope
    from paper_ingestion.models import ChunkForEmbedding
    from paper_ingestion.services import pdf_workflow as pdf_workflow_module
    from paper_ingestion.services.pdf_workflow import (
        _delete_reconcile_generation,
        reconcile_paper_embeddings,
    )
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams

    _bind_live_collection(monkeypatch, collection_name)
    qdrant = AsyncQdrantClient(url=qdrant_url, timeout=15)
    pool = SharedConnPool(contract_conn)
    collection_created = False
    try:
        await qdrant.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=len(_LIVE_VECTOR), distance=Distance.COSINE),
        )
        collection_created = True
        await ensure_visibility_payload_indexes(qdrant, collection_name=collection_name)

        generation = "1" * 32
        stale_generation = "0" * 31 + "1"
        await rotate_visibility_checkpoint(
            contract_conn,
            generation=generation,
            qdrant_recovery="live_test",
        )
        embedder = _LiveVisibilityEmbedder(qdrant, generation)

        caller = await seed_user_row(contract_conn, f"{collection_name}@contract.example.com")
        other = await seed_user_row(
            contract_conn,
            f"other-{collection_name}@contract.example.com",
        )

        public_repair = await _seed_paper(
            contract_conn,
            f"{collection_name}-public",
            visibility_scope="public",
            discovered_by=other,
        )
        private_library = await _seed_paper(
            contract_conn,
            f"{collection_name}-private-library",
            source_type="local",
        )
        unknown_library = await _seed_paper(
            contract_conn,
            f"{collection_name}-unknown-library",
            source_type="future_adapter",
        )
        private_unshelved = await _seed_paper(
            contract_conn,
            f"{collection_name}-private-unshelved",
            source_type="local",
        )
        audit_only = await _seed_paper(
            contract_conn,
            f"{collection_name}-audit-only",
            discovered_by=caller,
        )
        unknown_unshelved = await _seed_paper(
            contract_conn,
            f"{collection_name}-unknown-unshelved",
            source_type="future_adapter",
        )
        forged_public_payload = await _seed_paper(
            contract_conn,
            f"{collection_name}-forged",
            source_type="local",
        )
        mismatch = await _seed_paper(
            contract_conn,
            f"{collection_name}-mismatch",
            visibility_scope="public",
        )
        for paper_id in (private_library, unknown_library):
            await _add_to_library(contract_conn, caller, paper_id)

        repair_content = "stale generation payload repair"
        mismatch_content = "current mismatch content"
        repair_point_id = await _seed_visibility_chunk(contract_conn, public_repair, repair_content)
        mismatch_point_id = await _seed_visibility_chunk(contract_conn, mismatch, mismatch_content)

        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=public_repair,
            content=repair_content,
            source_type="arxiv",
            visibility_scope="private",
            visibility_generation=stale_generation,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=private_library,
            content="private library",
            source_type="local",
            visibility_scope="private",
            visibility_generation=generation,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=unknown_library,
            content="unknown source in library",
            source_type="future_adapter",
            visibility_scope="private",
            visibility_generation=generation,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=private_unshelved,
            content="private unshelved",
            source_type="local",
            visibility_scope="private",
            visibility_generation=generation,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=audit_only,
            content="audit only",
            source_type="arxiv",
            visibility_scope="private",
            visibility_generation=generation,
            legacy_user_id=caller,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=unknown_unshelved,
            content="unknown unshelved",
            source_type="future_adapter",
            visibility_scope="private",
            visibility_generation=generation,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=forged_public_payload,
            content="forged public payload",
            source_type="arxiv",
            visibility_scope="public",
            visibility_generation=generation,
            legacy_user_id=caller,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=mismatch,
            content=mismatch_content,
            source_type="arxiv",
            visibility_scope="public",
            visibility_generation=generation,
            fingerprint_content="superseded content",
        )

        library_ids = [private_library, unknown_library]
        initial_hits = await embedder.search_similar(
            "visibility",
            limit=100,
            score_threshold=0.0,
            user_id=caller,
            library_paper_ids=library_ids,
        )
        initial_ids = {int(hit["paper_id"]) for hit in initial_hits}
        assert public_repair not in initial_ids, "stale generations must under-fetch"
        assert forged_public_payload in initial_ids, "the relational recheck must see the forgery"
        assert not initial_ids & {private_unshelved, audit_only, unknown_unshelved}

        before_repair = (
            await qdrant.retrieve(
                collection_name=collection_name,
                ids=[repair_point_id],
                with_payload=True,
                with_vectors=True,
            )
        )[0]
        repaired = await reconcile_paper_embeddings(
            public_repair,
            pool,
            embedder,
        )
        after_repair = (
            await qdrant.retrieve(
                collection_name=collection_name,
                ids=[repair_point_id],
                with_payload=True,
                with_vectors=True,
            )
        )[0]
        assert repaired["status"] == "healthy"
        assert after_repair.vector == before_repair.vector
        assert after_repair.payload["visibility_scope"] == "public"
        assert after_repair.payload["visibility_generation"] == generation

        mismatch_result = await reconcile_paper_embeddings(mismatch, pool, embedder)
        mismatch_record = (
            await qdrant.retrieve(
                collection_name=collection_name,
                ids=[mismatch_point_id],
                with_payload=True,
                with_vectors=False,
            )
        )[0]
        from paper_ingestion.ingestion.embed_store import chunk_embedding_fingerprint

        assert mismatch_result["status"] == "repaired"
        assert mismatch_record.payload["embedding_fingerprint"] == chunk_embedding_fingerprint(
            mismatch_content
        )

        candidates = {
            public_repair,
            private_library,
            unknown_library,
            private_unshelved,
            audit_only,
            unknown_unshelved,
            forged_public_payload,
            mismatch,
        }
        expected_visible = {public_repair, private_library, unknown_library, mismatch}
        direct_visible = {
            paper_id
            for paper_id in candidates
            if await _layer_a_visible(contract_conn, paper_id, caller)
        }
        assert direct_visible == expected_visible
        assert await _feed_visible_ids(contract_conn, candidates, caller) == expected_visible

        cross_hits = await embedder.search_similar(
            "visibility",
            limit=100,
            score_threshold=0.0,
            user_id=caller,
            library_paper_ids=library_ids,
        )
        cross_ids = {int(hit["paper_id"]) for hit in cross_hits}
        assert forged_public_payload in cross_ids
        assert await _relationally_visible_ids(contract_conn, cross_ids, caller) == expected_visible

        explicit_hits = await embedder.search_chunks_global(
            "visibility",
            limit=100,
            score_threshold=0.0,
            scope=SearchScope.explicit_papers(caller, list(candidates), library_ids),
        )
        explicit_ids = {int(hit["paper_id"]) for hit in explicit_hits}
        assert (
            await _relationally_visible_ids(contract_conn, explicit_ids, caller) == expected_visible
        )

        single_visible: set[int] = set()
        for paper_id in candidates:
            if not await _layer_a_visible(contract_conn, paper_id, caller):
                continue
            hits = await embedder.search_chunks_in_paper(
                "visibility",
                paper_id,
                score_threshold=0.0,
                user_id=caller,
                library_paper_ids=library_ids,
            )
            if hits:
                single_visible.add(paper_id)
        assert single_visible == expected_visible

        restored_generation = "2" * 32
        restored_token = "restored-worker"
        await rotate_visibility_checkpoint(
            contract_conn,
            generation=restored_generation,
            qdrant_recovery="restored",
        )
        assert await claim_visibility_lease(
            contract_conn,
            generation=restored_generation,
            worker_token=restored_token,
        )
        assert await complete_visibility_checkpoint(
            contract_conn,
            generation=restored_generation,
            worker_token=restored_token,
        )
        restored_checkpoint = await load_visibility_checkpoint(contract_conn)
        assert restored_checkpoint is not None
        rotated = await validate_checkpoint_collection_pair(
            contract_conn,
            qdrant,
            restored_checkpoint,
            collection_name=collection_name,
        )
        assert rotated.status == "pending"
        assert rotated.visibility_generation != restored_generation
        assert rotated.qdrant_recovery == "collection_mismatch"

        old_generation = rotated.visibility_generation
        old_token = "stale-worker"
        assert await claim_visibility_lease(
            contract_conn,
            generation=old_generation,
            worker_token=old_token,
        )
        new_generation = "3" * 32
        await rotate_visibility_checkpoint(
            contract_conn,
            generation=new_generation,
            qdrant_recovery="newer_restore",
        )
        embedder.set_generation(new_generation)

        concurrent_paper = await _seed_paper(
            contract_conn,
            f"{collection_name}-concurrent",
            visibility_scope="public",
        )
        concurrent_content = "newer concurrent content"
        concurrent_point_id = await _seed_visibility_chunk(
            contract_conn,
            concurrent_paper,
            concurrent_content,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=concurrent_paper,
            content=concurrent_content,
            source_type="arxiv",
            visibility_scope="public",
            visibility_generation=new_generation,
        )
        reference_before = await contract_conn.fetchval(
            "SELECT embedding_id FROM paper_chunks WHERE paper_id = $1 AND chunk_index = 0",
            concurrent_paper,
        )
        with pytest.raises(StaleVisibilityLeaseError):
            await reconcile_paper_embeddings(
                concurrent_paper,
                pool,
                embedder,
                visibility_generation=old_generation,
                worker_lease_token=old_token,
            )
        concurrent_record = (
            await qdrant.retrieve(
                collection_name=collection_name,
                ids=[concurrent_point_id],
                with_payload=True,
                with_vectors=True,
            )
        )[0]
        reference_after = await contract_conn.fetchval(
            "SELECT embedding_id FROM paper_chunks WHERE paper_id = $1 AND chunk_index = 0",
            concurrent_paper,
        )
        assert concurrent_record.payload["visibility_generation"] == new_generation
        assert concurrent_record.vector == _LIVE_VECTOR
        assert reference_after == reference_before == concurrent_point_id

        race_token = "same-content-race"
        assert await claim_visibility_lease(
            contract_conn,
            generation=new_generation,
            worker_token=race_token,
        )
        race_paper = await _seed_paper(
            contract_conn,
            f"{collection_name}-same-content-race",
            visibility_scope="public",
        )
        race_content = "same content survives generation rotation"
        race_point_id = await _seed_visibility_chunk(
            contract_conn,
            race_paper,
            race_content,
        )
        await _upsert_visibility_point(
            qdrant,
            collection_name,
            paper_id=race_paper,
            content=race_content,
            source_type="arxiv",
            visibility_scope="public",
            visibility_generation=new_generation,
        )
        race_new_generation = "4" * 32

        async def _replace_after_lease_check(*_args: object, **_kwargs: object) -> bool:
            await rotate_visibility_checkpoint(
                contract_conn,
                generation=race_new_generation,
                qdrant_recovery="same_content_race",
            )
            await _upsert_visibility_point(
                qdrant,
                collection_name,
                paper_id=race_paper,
                content=race_content,
                source_type="arxiv",
                visibility_scope="public",
                visibility_generation=race_new_generation,
            )
            return True

        monkeypatch.setattr(
            pdf_workflow_module,
            "visibility_lease_is_current",
            _replace_after_lease_check,
        )
        await _delete_reconcile_generation(
            embedder,
            race_paper,
            [
                ChunkForEmbedding(
                    chunk_index=0,
                    content=race_content,
                    page_number=1,
                    start_char=0,
                    end_char=len(race_content),
                )
            ],
            conn=contract_conn,
            visibility_generation=new_generation,
            worker_lease_token=race_token,
        )
        race_record = (
            await qdrant.retrieve(
                collection_name=collection_name,
                ids=[race_point_id],
                with_payload=True,
                with_vectors=True,
            )
        )[0]
        assert race_record.payload["visibility_generation"] == race_new_generation
        assert race_record.vector == _LIVE_VECTOR
        assert (
            await contract_conn.fetchval(
                "SELECT embedding_id FROM paper_chunks WHERE paper_id = $1",
                race_paper,
            )
            == race_point_id
        )
    finally:
        if collection_created:
            await qdrant.delete_collection(collection_name=collection_name)
        await qdrant.close()


async def test_trusted_refresh_purges_seeded_content_when_adapter_value_is_null(
    contract_conn,
) -> None:
    """No-COALESCE: a NULL from the trusted adapter PURGES attacker-seeded content.

    ``_TRUSTED_REFRESH_CONFLICT`` overwrites descriptive columns unconditionally,
    never ``COALESCE(EXCLUDED.x, papers.x)``. A COALESCE would let an attacker's
    pre-seeded value survive whenever the verified adapter omits that column (its
    value is ``NULL``). This pins that the NULL wins, so a promoted row cannot
    retain foreign content the verified source did not supply.
    """
    from paper_ingestion.models.papers import PaperCreate, SourceType
    from paper_ingestion.services.pdf_workflow import (
        upsert_paper,
        upsert_verified_public_paper,
    )

    seeded = await upsert_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-null-purge",
            source_type=SourceType.LOCAL,
            title="Attacker title",
            authors=["Attacker"],
            abstract="attacker abstract",
            pdf_url="https://example.test/attacker-null.pdf",
            url="https://example.test/attacker-null-seed",
        ),
    )
    assert seeded["abstract"] == "attacker abstract"
    assert seeded["pdf_url"] == "https://example.test/attacker-null.pdf"

    promoted = await upsert_verified_public_paper(
        contract_conn,
        PaperCreate(
            external_id="upsert-null-purge",
            source_type=SourceType.ARXIV,
            title="Verified title",
            authors=["Verified Author"],
            url="https://arxiv.org/abs/null-purge",
            # abstract + pdf_url omitted -> None: the verified adapter supplies no
            # value, so the attacker-seeded content must be erased, not preserved.
        ),
        discovered_by=None,
    )
    assert promoted["id"] == seeded["id"]
    assert promoted["visibility_scope"] == "public"
    assert promoted["abstract"] is None, "NULL adapter abstract must purge attacker content"
    assert promoted["pdf_url"] is None, "NULL adapter pdf_url must purge attacker content"
