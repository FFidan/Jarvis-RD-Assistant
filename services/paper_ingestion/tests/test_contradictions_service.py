"""Tests for quote-verified cross-paper contradiction detection."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from jarvis_common.verify import QuoteVerifier
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.contradictions import (
    ContradictionCandidate,
    VerifiedFinding,
    _classify_candidate,
    _do_scan_contradictions,
    _load_verified_findings,
    _persist_contradiction,
    _polarity_score,
    aggregate_consensus,
    build_contradiction_candidates,
    list_contradictions,
    scan_contradictions,
)
from pydantic import ValidationError
from paper_ingestion.services.contradictions_extract import _parse_findings
from paper_ingestion.services.contradictions_persist import (
    _CONSENSUS_ROW_CAP,
    _find_existing_contradiction_id,
    _normalize_claim_topic,
)
from tests.conftest import FakeRecord, _make_pool_and_conn


def _stub_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acquired: bool = True,
) -> MagicMock:
    """Replace the scan lock with an async context manager of known state."""
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=acquired)
    lock.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "paper_ingestion.services.contradictions.AdvisoryLock",
        lambda *_args, **_kwargs: lock,
    )
    return lock


def test_parse_findings_accepts_json_string_fields():
    """Summary JSONB fields may arrive as strings in live contract fixtures."""

    row = FakeRecord(
        {
            "paper_id": 10,
            "title": "Paper",
            "key_findings": json.dumps(
                [{"finding": "A finding", "quote": "A quote", "page_number": 4}]
            ),
            "cross_references": json.dumps([{"related_paper_id": 11}]),
            "content_generation": 3,
        }
    )

    findings = _parse_findings(row)

    assert len(findings) == 1
    assert findings[0].paper_id == 10
    assert findings[0].quote == "A quote"
    assert findings[0].page_number == 4
    assert findings[0].cross_reference_ids == frozenset({11})
    assert findings[0].content_generation == 3


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
    """Semantically close findings can be ranked even without explicit cross-references.

    Uses paper_id=1 (single-paper mode) so the full O(n²) scan runs and
    semantic overlap without cross-references is surfaced.
    """
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

    candidates = build_contradiction_candidates(findings, paper_id=1)

    assert len(candidates) == 1
    assert "semantic:" in candidates[0].reason
    assert candidates[0].score > 0


def test_build_contradiction_candidates_keeps_three_letter_scientific_acronyms():
    """Acronym-heavy topics should still produce candidate overlap."""
    positive = VerifiedFinding(
        paper_id=1,
        title="A",
        finding="RAG GNN improves.",
        quote="RAG GNN improves.",
        page_number=1,
        cross_reference_ids=frozenset(),
    )
    negative = VerifiedFinding(
        paper_id=2,
        title="B",
        finding="RAG GNN reduces.",
        quote="RAG GNN reduces.",
        page_number=2,
        cross_reference_ids=frozenset(),
    )

    candidates = build_contradiction_candidates([positive, negative], paper_id=1)

    assert len(candidates) == 1
    assert "term_overlap" in candidates[0].reason
    assert candidates[0].score > 0


def test_build_contradiction_candidates_ignores_three_letter_noise_words():
    """Common short words should not create contradiction candidates by themselves."""
    positive = VerifiedFinding(
        paper_id=1,
        title="A",
        finding="The and for improves.",
        quote="The and for improves.",
        page_number=1,
        cross_reference_ids=frozenset(),
    )
    negative = VerifiedFinding(
        paper_id=2,
        title="B",
        finding="The and for reduces.",
        quote="The and for reduces.",
        page_number=2,
        cross_reference_ids=frozenset(),
    )

    assert build_contradiction_candidates([positive, negative], paper_id=1) == []


def test_build_contradiction_candidates_boosts_opposite_polarity():
    """Opposite-polarity cues are soft ranking signals, not hard filters.

    Uses paper_id=1 (single-paper mode) so the full O(n²) scan runs.
    """
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

    [candidate] = build_contradiction_candidates([positive, negative], paper_id=1)

    assert "opposite_polarity" in candidate.reason
    assert candidate.score > 0.25


def test_build_contradiction_candidates_downranks_same_polarity():
    """Same-polarity pairs remain candidates but score below opposite-polarity pairs.

    Uses paper_id=1 (single-paper mode) so the full O(n²) scan runs.
    """
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

    candidates = build_contradiction_candidates([base, same, opposite], paper_id=1)
    by_pair = {frozenset({c.a.paper_id, c.b.paper_id}): c for c in candidates}

    assert "same_polarity" in by_pair[frozenset({1, 2})].reason
    assert "opposite_polarity" in by_pair[frozenset({1, 3})].reason
    assert by_pair[frozenset({1, 3})].score > by_pair[frozenset({1, 2})].score


@pytest.mark.asyncio
async def test_scan_contradictions_persists_only_when_both_quotes_verify(monkeypatch):
    """LLM-positive candidates are inserted only after both quotes verify."""
    _stub_advisory_lock(monkeypatch)

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
    conn.fetchrow.side_effect = [None, FakeRecord({"id": 99})]

    async def fake_classify(
        _openai_client, _http_client, candidate: ContradictionCandidate, *, model: str
    ):
        assert candidate.a.paper_id == 1
        assert model
        return ContradictionClassification(
            is_contradiction=True,
            stance="opposes",
            claim_topic="effect of the method on accuracy",
            contradiction_type="result",
            explanation="The reported accuracy direction conflicts.",
            quote_a="Method improves accuracy.",
            quote_b="Method reduces accuracy.",
            confidence=0.91,
        )

    monkeypatch.setattr(
        "paper_ingestion.services.contradictions._classify_candidate",
        fake_classify,
    )

    result = await scan_contradictions(
        pool,
        AsyncMock(),
        QuoteVerifier(),
        openai_client=AsyncMock(),
        user_id=7,
    )

    assert result["contradictions_found"] == 1
    assert result["contradiction_ids"] == [99]
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_scan_contradictions_discards_unverified_llm_quotes(monkeypatch):
    """A candidate is not stored when the classifier returns unsupported quotes."""
    _stub_advisory_lock(monkeypatch)

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

    async def fake_classify(
        _openai_client, _http_client, _candidate: ContradictionCandidate, *, model: str
    ):
        return ContradictionClassification(
            is_contradiction=True,
            stance="opposes",
            claim_topic="whether the method conflicts",
            contradiction_type="direct",
            explanation="The reported findings directly conflict with each other.",
            quote_a="invented A",
            quote_b="actual B",
            confidence=0.9,
        )

    monkeypatch.setattr(
        "paper_ingestion.services.contradictions._classify_candidate",
        fake_classify,
    )

    result = await scan_contradictions(
        pool,
        AsyncMock(),
        QuoteVerifier(),
        openai_client=AsyncMock(),
        user_id=7,
    )

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

    rows, total = await list_contradictions(conn, user_id=1, paper_id=1)

    assert total == 1
    assert rows[0].id == 7
    assert rows[0].paper_a_title == "Paper A"
    assert "paper_a_id" in conn.fetch.await_args.args[0]


# ---------------------------------------------------------------------------
# A3.2 — polarity scoring: substring double-count and negation double-count
# ---------------------------------------------------------------------------


def test_polarity_does_not_double_count_substring():
    """'reduces error' must score +1: positive cue wins over negative 'reduces'.

    Old substring ``in`` check incremented both positive (via 'reduces error')
    AND negative (via 'reduces'), cancelling to 0.

    New implementation: greedy non-overlapping match selection picks the longer
    match ('reduces error', positive) and discards the shorter overlapping match
    ('reduces', negative) → positive=1, negative=0 → score +1.
    """
    # Multi-word positive cue must win over its shorter negative sub-match.
    score = _polarity_score("the new method reduces error rates")
    assert score == 1, f"Expected +1 ('reduces error' positive wins), got {score}"

    # Phrase with no ambiguity: two positive cues.
    assert _polarity_score("significant improvement in accuracy") == 1

    # Multi-word negative cue: 'no benefit' negative wins, 'benefit' positive excluded.
    assert _polarity_score("showed no benefit over baseline") == -1


def test_polarity_does_not_double_count_negation():
    """'not significant' scores -1: counted once via _NEGATIVE_RE_CUES, not twice.

    Old code: 'not significant' increments negative via _NEGATIVE_CUES substring,
    then 'not' also increments negative via _NEGATION_RE.findall → -2 raw count.
    New code: _NEGATION_RE augment is dropped entirely; 'not significant' matches
    as a single negative cue → raw negative count == 1, positive == 0 → score -1.
    """
    score = _polarity_score("the difference was not significant")
    assert score == -1, f"Expected -1 (one negative cue), got {score}"


def test_polarity_uses_word_boundaries():
    """Word-boundary lookarounds prevent partial-word matches.

    'boost-converter' contains 'boost' but the lookaround ``(?<!\\w)`` before
    'boost' requires a non-word character or start-of-string before the match.
    In 'boost-converter', 'boost' starts at position 0 (beginning of the string
    or after a space), so ``(?<!\\w)`` IS satisfied — 'boost' will match.
    However, 'significant' embedded mid-word (e.g. 'insignificant') should NOT
    match because it is preceded by 'in' (a word character).
    """
    # 'boost' at the start of a hyphenated word: does match (boundary is at position 0)
    score_boost = _polarity_score("the boost-converter improvement is significant")
    # 'improvement' and 'significant' are clear positive cues; 'boost' may also
    # match (3 positives) or not (2 positives) — either way score is +1.
    assert score_boost == 1, f"Expected +1 for clearly positive phrase, got {score_boost}"

    # 'insignificant' must NOT match 'significant' (preceded by word char 'n').
    score_insig = _polarity_score("the difference was insignificant")
    # No positive or negative cue should fire → score 0.
    assert score_insig == 0, (
        f"'insignificant' should not match 'significant' (word-boundary guard), got {score_insig}"
    )


# ---------------------------------------------------------------------------
# Cross-ref pre-filter for library-wide scans
# ---------------------------------------------------------------------------


def _make_finding(
    paper_id: int,
    *,
    cross_reference_ids: frozenset[int] = frozenset(),
    finding_suffix: str = "",
) -> VerifiedFinding:
    """Build a VerifiedFinding with unique but low-overlap text."""
    return VerifiedFinding(
        paper_id=paper_id,
        title=f"Paper {paper_id}{finding_suffix}",
        finding=f"Unique finding text for paper {paper_id} identifier_{paper_id}",
        quote=f"Unique quote for paper {paper_id} ref_{paper_id}",
        page_number=1,
        cross_reference_ids=cross_reference_ids,
    )


def test_pair_construction_uses_cross_ref_index_for_library_scan(monkeypatch):
    """Library-wide scan (paper_id=None) uses cross-ref pre-filter, NOT O(n²).

    Seed 100 findings where papers 1-5 cross-reference each other and the
    remaining 95 have no cross-references.  With the pre-filter, only pairs
    involving the 5 cross-referenced papers are scored.  The quadratic fallback
    (4950 pairs for 100 findings) must NOT occur.
    """
    # Papers 1–5 cross-reference each other (cross-ref pair pool = 5*4/2 = 10 pairs).
    cross_ref_ids = frozenset(range(1, 6))
    cross_findings = [
        _make_finding(pid, cross_reference_ids=cross_ref_ids - {pid}) for pid in range(1, 6)
    ]
    # 95 unrelated papers — no cross-references, text deliberately distinct.
    unrelated_findings = [
        _make_finding(pid, finding_suffix=f"_unrelated_{pid}") for pid in range(6, 101)
    ]
    all_findings = cross_findings + unrelated_findings

    jaccard_calls: list[tuple[set[str], set[str]]] = []

    from paper_ingestion.services import contradictions as _mod

    original_jaccard = _mod._jaccard

    def counting_jaccard(a: set[str], b: set[str]) -> float:
        jaccard_calls.append((a, b))
        return original_jaccard(a, b)

    monkeypatch.setattr(_mod, "_jaccard", counting_jaccard)

    candidates = build_contradiction_candidates(all_findings, paper_id=None)

    n = len(all_findings)
    quadratic_pairs = n * (n - 1) // 2  # 4950
    actual_calls = len(jaccard_calls)

    # Cross-ref pre-filter should produce far fewer calls than quadratic.
    assert actual_calls < quadratic_pairs // 10, (
        f"Expected far fewer than {quadratic_pairs} jaccard calls (got {actual_calls}); "
        "cross-ref pre-filter appears not to be active for library-wide scan."
    )
    # Cross-referenced papers should still surface as candidates.
    involved_paper_ids = {c.a.paper_id for c in candidates} | {c.b.paper_id for c in candidates}
    assert involved_paper_ids.issubset(set(range(1, 6))), (
        f"Only cross-referenced papers (1–5) should be candidates; got {involved_paper_ids}"
    )


def test_pair_construction_full_scan_when_paper_id_provided(monkeypatch):
    """Single-paper query (paper_id provided) still runs the full O(n²) scan.

    With paper_id=1 and 20 findings (5 cross-referenced, 15 unrelated),
    the scanner must evaluate ALL findings that include paper 1, not just
    cross-referenced pairs.
    """
    cross_ref_ids = frozenset(range(1, 6))
    cross_findings = [
        _make_finding(pid, cross_reference_ids=cross_ref_ids - {pid}) for pid in range(1, 6)
    ]
    # 15 additional findings with overlapping text so they pass the filter.
    overlap_findings = [
        VerifiedFinding(
            paper_id=100 + i,
            title=f"Related Paper {i}",
            finding=f"Unique finding text for paper 1 identifier_1 extra_{i}",
            quote=f"Unique quote for paper 1 ref_1 extra_{i}",
            page_number=1,
            cross_reference_ids=frozenset(),
        )
        for i in range(15)
    ]
    all_findings = cross_findings + overlap_findings

    jaccard_calls: list[tuple[set[str], set[str]]] = []

    from paper_ingestion.services import contradictions as _mod

    original_jaccard = _mod._jaccard

    def counting_jaccard(a: set[str], b: set[str]) -> float:
        jaccard_calls.append((a, b))
        return original_jaccard(a, b)

    monkeypatch.setattr(_mod, "_jaccard", counting_jaccard)

    # Query for paper_id=1 — should use full O(n²) loop over all findings that
    # include paper 1.
    candidates = build_contradiction_candidates(all_findings, paper_id=1)

    # With paper_id=1 and 20 findings, quadratic scan checks all pairs involving
    # paper 1: up to (n-1) = 19 pairs.  The cross-ref pre-filter would only check
    # 4 pairs (papers 2–5).  If jaccard was called for overlap_findings pairs,
    # the full scan is active.
    assert len(jaccard_calls) > 4, (
        f"Expected more than 4 jaccard calls for full O(n²) scan with paper_id=1, "
        f"got {len(jaccard_calls)}"
    )
    # paper_id=1 must appear in at least one candidate.
    involved_paper_ids = {c.a.paper_id for c in candidates} | {c.b.paper_id for c in candidates}
    assert 1 in involved_paper_ids, "paper_id=1 should be in at least one candidate"


def test_persist_contradiction_dedup_normalizes_legacy_rows_without_hashing():
    """The legacy-row lookup normalizes whitespace directly, without md5().

    Hashing parameterised quote bindings has no functional purpose and adds
    collision risk. Stored pre-normalization rows do need an explicit
    whitespace comparison so the first normalized write reuses them.
    """
    source = inspect.getsource(_find_existing_contradiction_id)
    assert "md5(" not in source, "The dedup lookup must compare normalized text rather than hashes."
    assert "regexp_replace(btrim(quote_a)" in source
    assert "regexp_replace(btrim(quote_b)" in source
    assert "= $3" in source
    assert "= $4" in source


# ---------------------------------------------------------------------------
# stance + claim_topic (consensus view)
# ---------------------------------------------------------------------------


def _make_candidate(paper_a_id: int = 1, paper_b_id: int = 2) -> ContradictionCandidate:
    return ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=paper_a_id,
            title="Paper A",
            finding="Finding A",
            quote="Quote A",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=paper_b_id,
            title="Paper B",
            finding="Finding B",
            quote="Quote B",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.8,
        reason="cross_reference",
    )


def test_supports_stance_requires_quotes():
    """A persisted stance (supports/opposes) must carry non-empty quotes."""
    with pytest.raises(ValidationError):
        ContradictionClassification(
            is_contradiction=False,
            stance="supports",
            claim_topic="whether X holds",
            explanation="Both findings affirm the claim.",
            quote_a="",
            quote_b="",
            confidence=0.7,
        )


def test_neutral_stance_allows_empty_quotes():
    """A 'neutral' stance is never persisted, so it does not require quotes."""
    model = ContradictionClassification(
        is_contradiction=False,
        stance="neutral",
        explanation="The findings differ only in scope.",
        confidence=0.4,
    )
    assert model.stance == "neutral"
    assert model.quote_a == "" and model.quote_b == ""


@pytest.mark.asyncio
async def test_classify_candidate_drops_neutral(monkeypatch):
    """_classify_candidate returns None for a neutral stance (not persisted)."""
    neutral = ContradictionClassification(
        is_contradiction=False,
        stance="neutral",
        explanation="The findings differ only in scope.",
        confidence=0.4,
    )

    async def fake_call(*_args, **_kwargs):
        return neutral

    monkeypatch.setattr("paper_ingestion.services.contradictions.call_llm_structured", fake_call)
    result = await _classify_candidate(AsyncMock(), AsyncMock(), _make_candidate(), model="m")
    assert result is None


@pytest.mark.asyncio
async def test_classify_candidate_keeps_supports(monkeypatch):
    """_classify_candidate keeps a 'supports' stance so it reaches verify/persist."""
    supports = ContradictionClassification(
        is_contradiction=False,
        stance="supports",
        claim_topic="whether X holds",
        explanation="Both findings affirm the claim.",
        quote_a="Paper A supports X.",
        quote_b="Paper B also supports X.",
        confidence=0.8,
    )

    async def fake_call(*_args, **_kwargs):
        return supports

    monkeypatch.setattr("paper_ingestion.services.contradictions.call_llm_structured", fake_call)
    result = await _classify_candidate(AsyncMock(), AsyncMock(), _make_candidate(), model="m")
    assert result is not None
    assert result.stance == "supports"


@pytest.mark.asyncio
async def test_persist_contradiction_writes_stance_and_claim_topic():
    """_persist_contradiction passes stance + claim_topic as INSERT bind params."""
    _, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [None, FakeRecord({"id": 55})]

    classification = ContradictionClassification(
        is_contradiction=False,
        stance="supports",
        claim_topic="whether X holds",
        explanation="Both findings affirm the claim.",
        quote_a="Quote A",
        quote_b="Quote B",
        confidence=0.8,
    )
    result = await _persist_contradiction(
        conn,
        _make_candidate(),
        classification,
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=7,
    )

    assert result == 55
    params = conn.fetchrow.await_args_list[1].args[1:]
    assert "supports" in params, f"expected stance in INSERT params, got {params}"
    assert "whether X holds" in params, f"expected claim_topic in INSERT params, got {params}"


# ---------------------------------------------------------------------------
# user_id scoping (cross-user isolation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_verified_findings_excludes_other_user():
    """_load_verified_findings passes user_id predicate in SQL.

    Pure SQL-capture unit: verifies that when user_id is passed, the generated
    query contains the user-scoping predicate.  No live DB required.
    """
    _, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    await _load_verified_findings(conn, paper_id=None, user_id=42)

    assert conn.fetch.await_count == 1
    args = conn.fetch.await_args.args[1:]

    # user_id=42 must be passed as a bind parameter (positional or keyword).
    # Behaviour-shape assertion: user_id reaches the DB layer as a parameter,
    # not interpolated into the SQL text. Statement-text shape is exercised by
    # the live-PG contract test (test_contradictions_contract.py).
    assert 42 in args, f"expected user_id=42 in bound params, got {args}"


@pytest.mark.asyncio
async def test_persist_contradiction_writes_user_id():
    """_persist_contradiction includes user_id in INSERT column list and params.

    Pure SQL-capture unit: verifies the INSERT statement contains user_id and
    that the value is passed as a bind parameter.
    """
    from paper_ingestion.services.contradiction_models import ContradictionClassification

    _, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [None, FakeRecord({"id": 77})]

    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=1,
            title="Paper A",
            finding="Finding A",
            quote="Quote A",
            page_number=1,
            cross_reference_ids=frozenset(),
            content_generation=3,
        ),
        b=VerifiedFinding(
            paper_id=2,
            title="Paper B",
            finding="Finding B",
            quote="Quote B",
            page_number=2,
            cross_reference_ids=frozenset(),
            content_generation=5,
        ),
        score=0.8,
        reason="cross_reference",
    )
    classification = ContradictionClassification(
        is_contradiction=True,
        stance="opposes",
        claim_topic="conflict topic",
        contradiction_type="result",
        explanation="Conflict explanation.",
        quote_a="Quote A",
        quote_b="Quote B",
        confidence=0.9,
    )

    result = await _persist_contradiction(
        conn,
        candidate,
        classification,
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=99,
    )

    assert result == 77
    assert conn.fetchrow.await_count == 2
    insert_call = conn.fetchrow.await_args_list[1]
    params = insert_call.args[1:]

    assert "paper_a_content_generation" in insert_call.args[0]
    assert "paper_b_content_generation" in insert_call.args[0]
    assert 99 in params, f"expected user_id=99 in INSERT params, got {params}"
    assert params[-2:] == (3, 5)


# ---------------------------------------------------------------------------
# Gap 4: contradiction_jobs user_id extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_id_payload, expected",
    [("42", 42), (42, 42)],
)
@pytest.mark.asyncio
async def test_contradiction_job_requires_and_converts_user_id(
    user_id_payload, expected, monkeypatch
):
    """_contradictions_scan_job requires owner identity and converts it to int."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.jobs import ProcrastinateJobContextShim
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    scan_mock = AsyncMock(return_value={"found": 0, "inserted": 0})
    pool = MagicMock()
    http_client = MagicMock()
    ctx = ProcrastinateJobContextShim(job_id="test-job", pool=AsyncMock())

    payload: dict = {"paper_id": None, "limit": 10, "user_id": user_id_payload}

    with (
        patch("paper_ingestion.contradiction_jobs.scan_contradictions", scan_mock),
        patch("paper_ingestion.contradiction_jobs.get_services") as get_svc_mock,
    ):
        svc = MagicMock()
        svc.verifier = MagicMock()
        svc.openai_client = MagicMock()
        get_svc_mock.return_value = svc

        result = await _contradictions_scan_job(pool, http_client, payload, ctx)

    scan_mock.assert_awaited_once()
    assert result == {"found": 0, "inserted": 0}
    assert scan_mock.call_args.kwargs["user_id"] == expected


@pytest.mark.asyncio
async def test_contradiction_job_rejects_a_payload_without_user_id():
    """A malformed internal job cannot start an ownerless scan."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.jobs import ProcrastinateJobContextShim
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    ctx = ProcrastinateJobContextShim(job_id="test-job", pool=AsyncMock())
    with patch("paper_ingestion.contradiction_jobs.get_services") as get_svc_mock:
        svc = MagicMock()
        svc.verifier = MagicMock()
        svc.openai_client = MagicMock()
        get_svc_mock.return_value = svc
        with pytest.raises(KeyError, match="user_id"):
            await _contradictions_scan_job(
                MagicMock(),
                MagicMock(),
                {"paper_id": None, "limit": 10},
                ctx,
            )


# ---------------------------------------------------------------------------
# Prompt-shape split: system carries rubric, user carries data only
# ---------------------------------------------------------------------------


def test_build_prompt_contains_no_instruction_head():
    """_build_prompt returns only wrapped data — no instruction prose.

    The contradiction-detection rubric now lives in _SYSTEM_CONTRADICTIONS
    (system role).  The user-role message returned by _build_prompt must not
    include the rubric keywords so it cannot be used to trick the LLM via
    prompt injection.
    """
    from paper_ingestion.services.contradictions import (
        ContradictionCandidate,
        VerifiedFinding,
        _build_prompt,
    )

    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=1,
            title="Paper A",
            finding="Method improves accuracy.",
            quote="Method improves accuracy.",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=2,
            title="Paper B",
            finding="Method reduces accuracy.",
            quote="Method reduces accuracy.",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.9,
        reason="cross_reference",
    )
    prompt = _build_prompt(candidate)

    assert "You are" not in prompt
    assert "Rules:" not in prompt
    assert "Do not invent" not in prompt
    assert "<title_a>" in prompt
    assert "<finding_a>" in prompt
    assert "<quote_a>" in prompt
    assert "<title_b>" in prompt
    assert "<finding_b>" in prompt
    assert "<quote_b>" in prompt


def test_system_contradictions_contains_rubric():
    """_SYSTEM_CONTRADICTIONS carries the full rubric in the system constant."""
    from paper_ingestion.services.contradictions import _SYSTEM_CONTRADICTIONS

    assert "You are" in _SYSTEM_CONTRADICTIONS
    assert "Rules:" in _SYSTEM_CONTRADICTIONS
    assert "Do not invent" in _SYSTEM_CONTRADICTIONS
    assert "is_contradiction" in _SYSTEM_CONTRADICTIONS


# ---------------------------------------------------------------------------
# T6.3 — per-user advisory-lock dedup: second concurrent enqueue short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_contradictions_dedup_returns_in_progress_when_locked(monkeypatch):
    """A second concurrent scan for the same user short-circuits via advisory lock.

    When AdvisoryLock returns False (lock already held by a running scan),
    scan_contradictions must return immediately with scan_already_in_progress=True
    and must not call _do_scan_contradictions or touch the DB.
    """
    from unittest.mock import patch

    pool = MagicMock()
    _stub_advisory_lock(monkeypatch, acquired=False)
    inner_scan = AsyncMock()

    with patch(
        "paper_ingestion.services.contradictions._do_scan_contradictions",
        inner_scan,
    ):
        result = await scan_contradictions(
            pool, AsyncMock(), QuoteVerifier(), openai_client=AsyncMock(), user_id=7
        )

    assert result == {"scan_already_in_progress": True}
    inner_scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_contradictions_dedup_proceeds_when_lock_acquired(monkeypatch):
    """scan_contradictions runs normally when the advisory lock is acquired.

    When AdvisoryLock returns True (no concurrent scan), the scan proceeds and
    returns the normal result dict (not the in-progress sentinel).
    """
    from unittest.mock import patch

    pool = MagicMock()
    _stub_advisory_lock(monkeypatch)

    expected = {
        "paper_id": None,
        "candidate_count": 0,
        "contradictions_found": 0,
        "contradiction_ids": [],
        "llm_failures": 0,
        "verification_failures": 0,
    }
    inner_scan = AsyncMock(return_value=expected)

    with patch(
        "paper_ingestion.services.contradictions._do_scan_contradictions",
        inner_scan,
    ):
        result = await scan_contradictions(
            pool, AsyncMock(), QuoteVerifier(), openai_client=AsyncMock(), user_id=7
        )

    assert result == expected
    assert "scan_already_in_progress" not in result
    inner_scan.assert_awaited_once()


# ---------------------------------------------------------------------------
# Total model failure fails the scan loudly instead of reporting empty success
# ---------------------------------------------------------------------------


def _summary_row(paper_id: int, cross_reference_ids: list[int]) -> FakeRecord:
    """One paper_summaries row with a single verified finding."""
    return FakeRecord(
        {
            "paper_id": paper_id,
            "title": f"Paper {paper_id}",
            "key_findings": [
                {
                    "finding": f"Method improves accuracy in paper {paper_id}.",
                    "quote": f"Method improves accuracy in paper {paper_id}.",
                    "page_number": 1,
                }
            ],
            "cross_references": [{"related_paper_id": rid} for rid in cross_reference_ids],
        }
    )


@pytest.mark.asyncio
async def test_do_scan_raises_when_every_candidate_fails_llm(monkeypatch):
    """All-candidate classifier failure raises so the job is marked failed."""
    pool, conn = _make_pool_and_conn()
    # One cross-referenced pair (1, 2) → exactly one candidate.
    conn.fetch.return_value = [_summary_row(1, [2]), _summary_row(2, [])]

    async def failing_classify(*_args, **_kwargs):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(
        "paper_ingestion.services.contradictions._classify_candidate",
        failing_classify,
    )

    with pytest.raises(RuntimeError, match="failed for all 1 candidate"):
        await _do_scan_contradictions(
            pool,
            AsyncMock(),
            QuoteVerifier(),
            openai_client=AsyncMock(),
            user_id=7,
        )


@pytest.mark.asyncio
async def test_do_scan_partial_llm_failure_still_returns_counts(monkeypatch):
    """A partial classifier failure does not raise and reports honest counts."""
    pool, conn = _make_pool_and_conn()
    # Paper 1 cross-references 2 and 3 → two candidates: (1,2) and (1,3).
    conn.fetch.return_value = [
        _summary_row(1, [2, 3]),
        _summary_row(2, []),
        _summary_row(3, []),
    ]

    calls = {"n": 0}

    async def flaky_classify(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model unreachable")
        return None  # neutral → skipped, not a failure

    monkeypatch.setattr(
        "paper_ingestion.services.contradictions._classify_candidate",
        flaky_classify,
    )

    result = await _do_scan_contradictions(
        pool,
        AsyncMock(),
        QuoteVerifier(),
        openai_client=AsyncMock(),
        user_id=7,
    )

    assert result["candidate_count"] == 2
    assert result["llm_failures"] == 1
    assert result["contradictions_found"] == 0


# ---------------------------------------------------------------------------
# _quotes_verify rejects empty-string quotes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote_a", "quote_b"),
    [
        ("", "some quote"),
        ("some quote", ""),
        ("   ", "  \t  "),
    ],
    ids=("empty-first-quote", "empty-second-quote", "whitespace-only"),
)
@pytest.mark.asyncio
async def test_quotes_verify_rejects_blank_quotes(quote_a: str, quote_b: str) -> None:
    """Blank model quotes are rejected before any source text is loaded."""
    from jarvis_common.verify import QuoteVerifier

    from paper_ingestion.services.contradictions import (
        ContradictionCandidate,
        VerifiedFinding,
        _quotes_verify,
    )

    _, conn = _make_pool_and_conn()
    verifier = QuoteVerifier()
    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=1,
            title="A",
            finding="f",
            quote="q",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=2,
            title="B",
            finding="f",
            quote="q",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.5,
        reason="test",
    )

    result = await _quotes_verify(
        conn,
        verifier,
        candidate,
        quote_a=quote_a,
        quote_b=quote_b,
    )
    assert result == (False, None, None)
    conn.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# _quotes_verify logs info when rejecting empty quotes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quotes_verify_logs_info_on_empty_quote(caplog) -> None:
    """_quotes_verify emits an info-level log when quote_a is empty.

    The log message must contain the candidate paper IDs (not the quote content).
    """
    from paper_ingestion.services.contradictions import (
        ContradictionCandidate,
        VerifiedFinding,
        _quotes_verify,
    )

    _, conn = _make_pool_and_conn()
    verifier = QuoteVerifier()
    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=11,
            title="A",
            finding="f",
            quote="q",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=22,
            title="B",
            finding="f",
            quote="q",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.5,
        reason="test",
    )

    with caplog.at_level("INFO", logger="paper_ingestion.services.contradictions"):
        result = await _quotes_verify(conn, verifier, candidate, quote_a="", quote_b="some quote")

    assert result == (False, None, None)

    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("quotes_verify_empty_quote" in r.message for r in info_records), (
        f"Expected quotes_verify_empty_quote in INFO logs, got: {[r.message for r in info_records]}"
    )
    # The log message must include the paper IDs (11 and 22), not the quote text.
    matching = [r for r in info_records if "quotes_verify_empty_quote" in r.message]
    assert matching, "No matching log record found"
    assert "11" in matching[0].message and "22" in matching[0].message, (
        f"Expected paper IDs in log message, got: {matching[0].message}"
    )


# ---------------------------------------------------------------------------
# aggregate_consensus — Unicode-aware clustering + honest truncation
# ---------------------------------------------------------------------------


def _consensus_row(
    *,
    stance: str,
    claim_topic: str,
    paper_a_id: int = 1,
    paper_b_id: int = 2,
    quote_a: str = "quote a",
    quote_b: str = "quote b",
) -> FakeRecord:
    return FakeRecord(
        {
            "stance": stance,
            "claim_topic": claim_topic,
            "paper_a_id": paper_a_id,
            "paper_b_id": paper_b_id,
            "quote_a": quote_a,
            "quote_b": quote_b,
            "page_a": 1,
            "page_b": 2,
            "paper_a_title": "Paper A",
            "paper_b_title": "Paper B",
        }
    )


@pytest.mark.asyncio
async def test_aggregate_consensus_keeps_distinct_non_latin_topics_separate():
    """Two unrelated non-Latin claim topics must cluster separately, not merge.

    Before the Unicode-aware fix, `_CLAIM_TOPIC_PUNCT_RE = re.compile(r"[^a-z0-9]+")`
    treated every character outside ASCII a-z0-9 as punctuation, so a Cyrillic,
    CJK, Arabic, Greek, or Hebrew claim_topic collapsed to "" and merged with
    every other non-Latin topic into a single cluster, silently summing their
    supports/opposes counts together.
    """
    _, conn = _make_pool_and_conn(
        fetch_return=[
            _consensus_row(
                stance="supports", claim_topic="Эффект температуры на прочность", paper_b_id=2
            ),
            _consensus_row(
                stance="opposes", claim_topic="Устойчивость данных при сжатии", paper_b_id=3
            ),
        ]
    )

    claims, truncated = await aggregate_consensus(conn, user_id=1)

    assert not truncated
    topics = {claim.claim_topic for claim in claims}
    assert len(claims) == 2, f"unrelated non-Latin topics must not merge: {topics}"
    # The set above holds display topics, not cluster keys, so it cannot show a
    # collapse on its own. Normalizing directly is what proves the key survives.
    assert _normalize_claim_topic("Устойчивость данных при сжатии"), (
        "a non-Latin topic must not normalize to an empty cluster key"
    )


@pytest.mark.asyncio
async def test_aggregate_consensus_reports_truncated_without_changing_total():
    """`truncated` signals a hit row cap; `total` (== len(claims)) keeps its meaning.

    Regression for a truncated evidence set silently presented as complete: the
    router computed `total` only from the returned clusters, with no signal that
    the underlying row cap had already dropped evidence before clustering.
    """
    rows = [_consensus_row(stance="supports", claim_topic=f"topic {i}") for i in range(1001)]
    _, conn = _make_pool_and_conn(fetch_return=rows)

    claims, truncated = await aggregate_consensus(conn, user_id=1, limit=2000)

    assert truncated, "1001 underlying rows must set truncated=True"
    assert len(claims) == 1000, "the 1001st row must be dropped after the cap, not clustered"
    # The connection double answers with these rows whatever it is asked for, so
    # the flag can only ever fire if the query really requests one row beyond the
    # cap. Without this, an off-by-one there silences truncation for good and
    # every assertion above still passes.
    assert conn.fetch.await_args_list[0].args[2] == _CONSENSUS_ROW_CAP + 1, (
        "the query must fetch one row past the cap to detect truncation at all"
    )


@pytest.mark.asyncio
async def test_aggregate_consensus_not_truncated_under_the_cap():
    """`truncated` stays False when the underlying set does not hit the cap."""
    rows = [_consensus_row(stance="supports", claim_topic=f"topic {i}") for i in range(5)]
    _, conn = _make_pool_and_conn(fetch_return=rows)

    claims, truncated = await aggregate_consensus(conn, user_id=1, limit=50)

    assert not truncated
    assert len(claims) == 5


# ---------------------------------------------------------------------------
# _persist_contradiction — whitespace-insensitive quote dedup
# ---------------------------------------------------------------------------


def _supports_classification(quote_a: str, quote_b: str) -> ContradictionClassification:
    return ContradictionClassification(
        is_contradiction=False,
        stance="supports",
        claim_topic="whether X holds",
        explanation="Both findings affirm the claim.",
        quote_a=quote_a,
        quote_b=quote_b,
        confidence=0.8,
    )


@pytest.mark.asyncio
async def test_persist_contradiction_normalizes_whitespace_before_binding():
    """Two verbatim quotes differing only in incidental whitespace must bind
    the identical value, so they hash to the same unique-index key instead of
    creating a second row.
    """
    _, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        None,
        FakeRecord({"id": 1}),
        None,
        FakeRecord({"id": 2}),
    ]

    await _persist_contradiction(
        conn,
        _make_candidate(),
        _supports_classification("Paper A  says   X.", "Paper B says not X."),
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=7,
    )
    first_quote_a = conn.fetchrow.await_args_list[1].args[5]

    await _persist_contradiction(
        conn,
        _make_candidate(),
        _supports_classification("Paper A says X.", "Paper B says not X."),
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=7,
    )
    second_quote_a = conn.fetchrow.await_args_list[3].args[5]

    assert first_quote_a == second_quote_a == "Paper A says X.", (
        f"whitespace-only variants must normalize to the same bound value: "
        f"{first_quote_a!r} vs {second_quote_a!r}"
    )


@pytest.mark.asyncio
async def test_persist_contradiction_fallback_select_matches_insert_normalization():
    """The conflict-fallback SELECT must bind the same normalized quotes as the
    INSERT it followed. If the two disagree, the fallback stops finding the row
    it should and creates a duplicate instead of preventing one.
    """
    _, conn = _make_pool_and_conn(
        fetchrow_side_effects=[None, asyncpg.UniqueViolationError(), FakeRecord({"id": 9})]
    )

    result = await _persist_contradiction(
        conn,
        _make_candidate(),
        _supports_classification("Paper A  says\tX.", "Paper B   says not   X."),
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=7,
    )

    assert result == 9
    insert_args = conn.fetchrow.await_args_list[1].args
    select_args = conn.fetchrow.await_args_list[2].args
    assert insert_args[5] == select_args[3] == "Paper A says X."
    assert insert_args[6] == select_args[4] == "Paper B says not X."


@pytest.mark.asyncio
async def test_persist_contradiction_reuses_legacy_whitespace_variant_before_insert():
    """A pre-upgrade raw quote row must prevent a normalized duplicate write."""
    _, conn = _make_pool_and_conn(fetchrow_return=FakeRecord({"id": 17}))

    result = await _persist_contradiction(
        conn,
        _make_candidate(),
        _supports_classification("Paper A says X.", "Paper B says not X."),
        page_a=1,
        page_b=2,
        model="test-model",
        user_id=7,
    )

    assert result == 17
    conn.fetchrow.assert_awaited_once()
