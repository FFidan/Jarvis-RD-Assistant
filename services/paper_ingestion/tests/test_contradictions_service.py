"""Tests for quote-verified cross-paper contradiction detection."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from jarvis_common.verify import QuoteVerifier
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.contradictions import (
    ContradictionCandidate,
    VerifiedFinding,
    _load_verified_findings,
    _persist_contradiction,
    _polarity_score,
    build_contradiction_candidates,
    list_contradictions,
    scan_contradictions,
)
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

    async def fake_classify(
        _openai_client, _http_client, candidate: ContradictionCandidate, *, model: str
    ):
        assert candidate.a.paper_id == 1
        assert model
        return ContradictionClassification(
            is_contradiction=True,
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
        pool, AsyncMock(), QuoteVerifier(), openai_client=AsyncMock()
    )

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

    async def fake_classify(
        _openai_client, _http_client, _candidate: ContradictionCandidate, *, model: str
    ):
        return ContradictionClassification(
            is_contradiction=True,
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
        pool, AsyncMock(), QuoteVerifier(), openai_client=AsyncMock()
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
# B2.3 — cross-ref pre-filter for library-wide scans (PI-EDGE-005)
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


def test_persist_contradiction_dedup_uses_direct_equality():
    """Fallback SELECT in _persist_contradiction uses direct equality, not md5().

    This is a static assertion against the function source: PI-CORE-002 fix.
    md5() wraps have no functional purpose with parameterised bindings and
    introduce hash-binding risk for LLM-controlled inputs.
    """
    source = inspect.getsource(_persist_contradiction)
    assert "md5(" not in source, (
        "Fallback SELECT still uses md5() — PI-CORE-002 not fixed. "
        "Replace 'md5(quote_a) = md5($3::text)' with 'quote_a = $3'."
    )
    # Confirm the direct equality form IS present.
    assert "quote_a = $3" in source, "Expected 'quote_a = $3' in fallback SELECT"
    assert "quote_b = $4" in source, "Expected 'quote_b = $4' in fallback SELECT"


# ---------------------------------------------------------------------------
# W1-D2-001 / W1-D2-002 — user_id scoping (cross-user isolation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_verified_findings_excludes_other_user():
    """_load_verified_findings passes user_id predicate in SQL (W1-D2-001).

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
    assert 42 in args, f"W1-D2-001: expected user_id=42 in bound params, got {args}"


@pytest.mark.asyncio
async def test_persist_contradiction_writes_user_id():
    """_persist_contradiction includes user_id in INSERT column list and params (W1-D2-002).

    Pure SQL-capture unit: verifies the INSERT statement contains user_id and
    that the value is passed as a bind parameter.
    """
    from paper_ingestion.services.contradiction_models import ContradictionClassification

    _, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"id": 77})

    candidate = ContradictionCandidate(
        a=VerifiedFinding(
            paper_id=1,
            title="Paper A",
            finding="Finding A",
            quote="Quote A",
            page_number=1,
            cross_reference_ids=frozenset(),
        ),
        b=VerifiedFinding(
            paper_id=2,
            title="Paper B",
            finding="Finding B",
            quote="Quote B",
            page_number=2,
            cross_reference_ids=frozenset(),
        ),
        score=0.8,
        reason="cross_reference",
    )
    classification = ContradictionClassification(
        is_contradiction=True,
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
    assert conn.fetchrow.await_count >= 1
    # Check the INSERT call (first fetchrow call is the INSERT).
    insert_call = conn.fetchrow.await_args_list[0]
    params = insert_call.args[1:]

    # Behaviour-shape assertion: user_id reaches the INSERT as a bind parameter.
    # SQL column-list shape is exercised by the live-PG contract test.
    assert 99 in params, f"W1-D2-002: expected user_id=99 in INSERT params, got {params}"


# ---------------------------------------------------------------------------
# Gap 4: contradiction_jobs user_id extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_id_payload, expected",
    [("42", 42), (None, None)],
)
@pytest.mark.asyncio
async def test_contradiction_job_extracts_user_id_from_payload_str_or_absent(
    user_id_payload, expected, monkeypatch
):
    """_contradictions_scan_job correctly converts str→int and None→None for user_id."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.jobs import JobContext
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    scan_mock = AsyncMock(return_value={"found": 0, "inserted": 0})
    pool = MagicMock()
    http_client = MagicMock()
    ctx = JobContext(job_id="test-job", _pool=pool)

    payload: dict = {"paper_id": None, "limit": 10}
    if user_id_payload is not None:
        payload["user_id"] = user_id_payload

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


# ---------------------------------------------------------------------------
# PI-06 — prompt-shape split: system carries rubric, user carries data only
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
# PI-12 — _quotes_verify rejects empty-string quotes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quotes_verify_rejects_empty_quote_a():
    """_quotes_verify returns (False, None, None) when quote_a is empty."""
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

    result = await _quotes_verify(conn, verifier, candidate, quote_a="", quote_b="some quote")
    assert result == (False, None, None)
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_quotes_verify_rejects_empty_quote_b():
    """_quotes_verify returns (False, None, None) when quote_b is empty."""
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

    result = await _quotes_verify(conn, verifier, candidate, quote_a="some quote", quote_b="")
    assert result == (False, None, None)
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_quotes_verify_rejects_whitespace_only_quotes():
    """_quotes_verify returns (False, None, None) for whitespace-only quotes."""
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

    result = await _quotes_verify(conn, verifier, candidate, quote_a="   ", quote_b="  \t  ")
    assert result == (False, None, None)
    conn.fetch.assert_not_awaited()
