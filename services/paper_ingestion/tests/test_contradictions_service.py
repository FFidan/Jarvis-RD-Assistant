"""Tests for quote-verified cross-paper contradiction detection."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from paper_ingestion.services.contradictions import (
    ContradictionCandidate,
    VerifiedFinding,
    build_contradiction_candidates,
    list_contradictions,
    scan_contradictions,
)
from paper_ingestion.verification import QuoteVerifier
from tests.conftest import FakeRecord, _make_pool_and_conn


def test_build_contradiction_candidates_prefers_cross_references():
    """Candidate narrowing uses existing cross-reference links."""
    findings = [
        VerifiedFinding(
            paper_id=1,
            title="Paper A",
            finding="Method improves accuracy on benchmark",
            quote="improves accuracy",
            page_number=1,
            cross_reference_ids=frozenset({2}),
        ),
        VerifiedFinding(
            paper_id=2,
            title="Paper B",
            finding="Method reduces accuracy on benchmark",
            quote="reduces accuracy",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        VerifiedFinding(
            paper_id=3,
            title="Unrelated",
            finding="Different topic",
            quote="different",
            page_number=3,
            cross_reference_ids=frozenset(),
        ),
    ]

    candidates = build_contradiction_candidates(findings)

    assert len(candidates) == 1
    assert candidates[0].a.paper_id == 1
    assert candidates[0].b.paper_id == 2
    assert "cross_reference" in candidates[0].reason


def test_build_contradiction_candidates_uses_semantic_overlap_without_cross_reference():
    """Semantically close findings can be ranked even without explicit cross-references."""
    findings = [
        VerifiedFinding(
            paper_id=1,
            title="Attention Scaling",
            finding="Attention pruning improves transformer latency on benchmark workloads.",
            quote="Attention pruning improves transformer latency.",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        VerifiedFinding(
            paper_id=2,
            title="Transformer Throughput",
            finding="Transformer attention pruning reduced benchmark latency in deployment.",
            quote="Transformer attention pruning reduced latency.",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
    ]

    candidates = build_contradiction_candidates(findings)

    assert len(candidates) == 1
    assert "semantic:" in candidates[0].reason
    assert candidates[0].score > 0


def test_build_contradiction_candidates_boosts_opposite_polarity():
    """Opposite-polarity cues are soft ranking signals, not hard filters."""
    positive = VerifiedFinding(
        paper_id=1,
        title="Method A",
        finding="The method improves accuracy on the shared benchmark.",
        quote="The method improves accuracy.",
        page_number=1,
        cross_reference_ids=frozenset(),
    )
    negative = VerifiedFinding(
        paper_id=2,
        title="Method B",
        finding="The method reduces accuracy on the shared benchmark.",
        quote="The method reduces accuracy.",
        page_number=2,
        cross_reference_ids=frozenset(),
    )

    [candidate] = build_contradiction_candidates([positive, negative])

    assert "opposite_polarity" in candidate.reason
    assert candidate.score > 0.25


def test_build_contradiction_candidates_downranks_same_polarity():
    """Same-polarity pairs remain candidates but score below opposite-polarity pairs."""
    base = VerifiedFinding(
        paper_id=1,
        title="Method A",
        finding="The method improves accuracy on the shared benchmark.",
        quote="The method improves accuracy.",
        page_number=1,
        cross_reference_ids=frozenset(),
    )
    same = VerifiedFinding(
        paper_id=2,
        title="Method B",
        finding="The method improves benchmark accuracy in deployment.",
        quote="The method improves benchmark accuracy.",
        page_number=2,
        cross_reference_ids=frozenset(),
    )
    opposite = VerifiedFinding(
        paper_id=3,
        title="Method C",
        finding="The method reduces benchmark accuracy in deployment.",
        quote="The method reduces benchmark accuracy.",
        page_number=3,
        cross_reference_ids=frozenset(),
    )

    candidates = build_contradiction_candidates([base, same, opposite])
    by_pair = {frozenset({c.a.paper_id, c.b.paper_id}): c for c in candidates}

    assert "same_polarity" in by_pair[frozenset({1, 2})].reason
    assert "opposite_polarity" in by_pair[frozenset({1, 3})].reason
    assert by_pair[frozenset({1, 3})].score > by_pair[frozenset({1, 2})].score


@pytest.mark.asyncio
async def test_scan_contradictions_persists_only_when_both_quotes_verify(monkeypatch):
    """LLM-positive candidates are inserted only after both quotes verify."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [
            FakeRecord(
                {
                    "paper_id": 1,
                    "title": "Paper A",
                    "key_findings": [
                        {
                            "finding": "Method improves accuracy.",
                            "quote": "Method improves accuracy.",
                            "page_number": 1,
                        }
                    ],
                    "cross_references": [{"related_paper_id": 2}],
                }
            ),
            FakeRecord(
                {
                    "paper_id": 2,
                    "title": "Paper B",
                    "key_findings": [
                        {
                            "finding": "Method reduces accuracy.",
                            "quote": "Method reduces accuracy.",
                            "page_number": 2,
                        }
                    ],
                    "cross_references": [],
                }
            ),
        ],
        [
            FakeRecord(
                {
                    "id": 10,
                    "paper_id": 1,
                    "chunk_index": 0,
                    "content": "Method improves accuracy.",
                    "page_number": 1,
                    "start_char": None,
                    "end_char": None,
                    "embedding_id": None,
                    "created_at": datetime.now(UTC),
                }
            )
        ],
        [
            FakeRecord(
                {
                    "id": 20,
                    "paper_id": 2,
                    "chunk_index": 0,
                    "content": "Method reduces accuracy.",
                    "page_number": 2,
                    "start_char": None,
                    "end_char": None,
                    "embedding_id": None,
                    "created_at": datetime.now(UTC),
                }
            )
        ],
    ]
    conn.fetchrow.return_value = FakeRecord({"id": 99})

    async def fake_classify(_http_client, candidate: ContradictionCandidate, *, model: str):
        assert candidate.a.paper_id == 1
        assert model
        return {
            "is_contradiction": True,
            "contradiction_type": "result",
            "explanation": "The reported accuracy direction conflicts.",
            "quote_a": "Method improves accuracy.",
            "quote_b": "Method reduces accuracy.",
            "confidence": 0.91,
        }

    monkeypatch.setattr(
        "paper_ingestion.services.contradictions._classify_candidate",
        fake_classify,
    )

    result = await scan_contradictions(pool, AsyncMock(), QuoteVerifier())

    assert result["contradictions_found"] == 1
    assert result["contradiction_ids"] == [99]
    assert conn.fetchrow.await_count == 1
    assert "INSERT INTO paper_contradictions" in conn.fetchrow.await_args.args[0]


@pytest.mark.asyncio
async def test_scan_contradictions_discards_unverified_llm_quotes(monkeypatch):
    """A candidate is not stored when the classifier returns unsupported quotes."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [
            FakeRecord(
                {
                    "paper_id": 1,
                    "title": "Paper A",
                    "key_findings": [{"finding": "A", "quote": "actual A", "page_number": 1}],
                    "cross_references": [{"related_paper_id": 2}],
                }
            ),
            FakeRecord(
                {
                    "paper_id": 2,
                    "title": "Paper B",
                    "key_findings": [{"finding": "B", "quote": "actual B", "page_number": 2}],
                    "cross_references": [],
                }
            ),
        ],
        [
            FakeRecord(
                {
                    "id": 10,
                    "paper_id": 1,
                    "chunk_index": 0,
                    "content": "actual A",
                    "page_number": 1,
                    "start_char": None,
                    "end_char": None,
                    "embedding_id": None,
                    "created_at": datetime.now(UTC),
                }
            )
        ],
        [
            FakeRecord(
                {
                    "id": 20,
                    "paper_id": 2,
                    "chunk_index": 0,
                    "content": "actual B",
                    "page_number": 2,
                    "start_char": None,
                    "end_char": None,
                    "embedding_id": None,
                    "created_at": datetime.now(UTC),
                }
            )
        ],
    ]

    async def fake_classify(_http_client, _candidate: ContradictionCandidate, *, model: str):
        return {
            "is_contradiction": True,
            "quote_a": "invented A",
            "quote_b": "actual B",
            "confidence": 0.9,
        }

    monkeypatch.setattr(
        "paper_ingestion.services.contradictions._classify_candidate",
        fake_classify,
    )

    result = await scan_contradictions(pool, AsyncMock(), QuoteVerifier())

    assert result["contradictions_found"] == 0
    assert result["verification_failures"] == 1
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_contradictions_maps_rows():
    """The list helper returns API response models and a total count."""
    conn = AsyncMock()
    conn.fetch.return_value = [
        FakeRecord(
            {
                "id": 7,
                "paper_a_id": 1,
                "paper_b_id": 2,
                "paper_a_title": "Paper A",
                "paper_b_title": "Paper B",
                "finding_a": "A",
                "finding_b": "B",
                "quote_a": "quote A",
                "quote_b": "quote B",
                "page_a": 1,
                "page_b": 2,
                "contradiction_type": "direct",
                "explanation": "Conflict",
                "confidence": 0.8,
                "status": "verified",
                "created_at": datetime.now(UTC),
                "total_count": 1,
            }
        )
    ]

    rows, total = await list_contradictions(conn, paper_id=1)

    assert total == 1
    assert rows[0].id == 7
    assert rows[0].paper_a_title == "Paper A"
    assert "paper_a_id" in conn.fetch.await_args.args[0]
