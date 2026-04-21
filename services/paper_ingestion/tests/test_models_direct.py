"""Direct tests for paper_ingestion Pydantic models and helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from paper_ingestion.models import (  # noqa: E402
    AskRequest,
    AskResponse,
    AskSourceItem,
    CitationGraphResponse,
    Confidence,
    CrossPaperAskRequest,
    CrossReference,
    DashboardMetrics,
    DiscoverRequest,
    ExtractionField,
    FeedPaper,
    FeedResponse,
    GraphEdge,
    GraphNode,
    KeyFinding,
    NoteCreate,
    PaperDetailResponse,
    PaperResponse,
    SearchRequest,
    SourceType,
    SummaryResponse,
    TopicCreate,
    TopicResponse,
    TopicUpdate,
    UserStateUpsert,
    compute_priority,
    priority_level,
)
from pydantic import ValidationError


def test_paper_response_accepts_local_urls():
    """PaperResponse accepts local:// URLs used for locally ingested PDFs."""
    paper = PaperResponse(
        id=1,
        external_id="local-1",
        source_type=SourceType.LOCAL,
        title="Local Paper",
        authors=["Ada"],
        abstract=None,
        published_date=date(2026, 1, 1),
        url="local://paper.pdf",
        pdf_url="https://example.com/paper.pdf",
        created_at=datetime.now(UTC),
    )

    assert paper.url == "local://paper.pdf"


def test_paper_response_rejects_non_http_urls():
    """PaperResponse rejects schemes outside the documented allowlist."""
    with pytest.raises(ValidationError, match="URL must start"):
        PaperResponse(
            id=1,
            external_id="paper-1",
            source_type=SourceType.ARXIV,
            title="Paper",
            authors=["Ada"],
            url="ftp://example.com/paper.pdf",
            created_at=datetime.now(UTC),
        )


def test_summary_response_parses_nested_findings():
    """SummaryResponse keeps verified findings and cross references typed."""
    summary = SummaryResponse(
        id=1,
        paper_id=10,
        summary_brief="Brief",
        summary_detailed="Detailed",
        key_findings=[
            KeyFinding(
                finding="Improved recall",
                quote="We improve recall by 10%.",
                page_number=3,
                verified=True,
            )
        ],
        confidence=Confidence.HIGH,
        cross_references=[
            CrossReference(
                related_paper_id=11,
                relationship="extends",
                explanation="Builds on the baseline",
            )
        ],
        created_at=datetime.now(UTC),
    )

    assert summary.key_findings[0].verified is True
    assert summary.cross_references[0].relationship == "extends"


def test_user_state_upsert_enforces_rating_bounds():
    """UserStateUpsert rejects ratings outside the UI's 1-5 range."""
    with pytest.raises(ValidationError, match="less than or equal to 5"):
        UserStateUpsert(rating=6)


def test_feed_response_defaults_search_mode():
    """FeedResponse keeps its default search mode when the caller omits it."""
    response = FeedResponse(
        papers=[
            FeedPaper(
                id=1,
                external_id="paper-1",
                source_type=SourceType.ARXIV,
                title="Paper",
                authors=["Ada"],
                url="https://example.com/paper",
                created_at=datetime.now(UTC),
            )
        ],
        total=1,
    )

    assert response.search_mode == "filtered"


def test_dashboard_metrics_defaults_to_onboarding_stage():
    """DashboardMetrics defaults to the documented onboarding stage."""
    metrics = DashboardMetrics(
        total_papers=0,
        unread_papers=0,
        pending_papers=0,
        due_cards=0,
        active_projects=0,
        topic_count=0,
        nudge_count=0,
    )

    assert metrics.onboarding_stage == "needs_topics"


def test_paper_detail_response_uses_empty_chunk_default():
    """PaperDetailResponse uses a concrete empty list for chunks by default."""
    detail = PaperDetailResponse(
        paper=PaperResponse(
            id=1,
            external_id="paper-1",
            source_type=SourceType.ARXIV,
            title="Paper",
            authors=["Ada"],
            url="https://example.com/paper",
            created_at=datetime.now(UTC),
        )
    )

    assert detail.chunks == []


def test_paper_detail_response_isolates_chunk_defaults():
    """PaperDetailResponse instances should not share their chunks list."""
    paper = PaperResponse(
        id=1,
        external_id="paper-1",
        source_type=SourceType.ARXIV,
        title="Paper",
        authors=["Ada"],
        url="https://example.com/paper",
        created_at=datetime.now(UTC),
    )
    first = PaperDetailResponse(paper=paper)
    second = PaperDetailResponse(paper=paper)
    first.chunks.append({"id": 1})

    assert second.chunks == []


def test_compute_priority_blends_relevance_recency_and_citations():
    """compute_priority returns a bounded blended score."""
    now = datetime.now(UTC)
    score = compute_priority(
        relevance_scores=[0.8, 0.6],
        discovered_at=now - timedelta(days=5),
        citation_count=40,
        now=now,
    )

    assert score == 0.73


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (None, "unscored"),
        (0.8, "must-read"),
        (0.5, "recommended"),
        (0.2, "background"),
    ],
)
def test_priority_level_buckets(score, expected):
    """priority_level maps the score thresholds used in the feed UI."""
    assert priority_level(score) == expected


def test_note_create_requires_positive_page_numbers():
    """NoteCreate rejects zero page numbers because user-facing pages are 1-indexed."""
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        NoteCreate(user_note="Important", page_number=0)


def test_ask_request_and_topic_create_validate_bounds():
    """Ask and topic request models enforce their documented input requirements."""
    ask = AskRequest(question="What changed?", max_chunks=3)
    topic = TopicCreate(name="Retrieval", query_terms=["retrieval", "rag"])

    assert ask.max_chunks == 3
    assert topic.enabled is True


def test_topic_create_rejects_empty_query_terms():
    """TopicCreate should not accept a topic that can never produce a search query."""
    with pytest.raises(ValidationError, match="at least 1 item"):
        TopicCreate(name="Retrieval", query_terms=[])


@pytest.mark.parametrize("query_terms", [[""], ["   "], ["rag", "   "]])
def test_topic_models_reject_blank_query_terms(query_terms):
    """Topic create and update models reject blank query terms."""
    with pytest.raises(ValidationError, match="must not contain blank strings"):
        TopicCreate(name="Retrieval", query_terms=query_terms)

    with pytest.raises(ValidationError, match="must not contain blank strings"):
        TopicUpdate(query_terms=query_terms)


def test_topic_models_strip_surrounding_whitespace():
    """Topic models normalize surrounding whitespace on valid query terms."""
    create = TopicCreate(name="Retrieval", query_terms=[" rag ", "agents  "])
    update = TopicUpdate(query_terms=["  ml ", "systems"])

    assert create.query_terms == ["rag", "agents"]
    assert update.query_terms == ["ml", "systems"]


def test_search_request_rejects_invalid_bounds():
    """SearchRequest enforces the query and max_results bounds (le=200)."""
    with pytest.raises(ValidationError):
        SearchRequest(query="")
    with pytest.raises(ValidationError):
        SearchRequest(query="x", max_results=201)


def test_cross_paper_ask_request_rejects_invalid_bounds():
    """CrossPaperAskRequest enforces chunk and paper limits."""
    with pytest.raises(ValidationError):
        CrossPaperAskRequest(question="What changed?", max_chunks=0)
    with pytest.raises(ValidationError):
        CrossPaperAskRequest(question="What changed?", max_papers=16)


def test_discover_request_rejects_out_of_range_values():
    """DiscoverRequest rejects invalid limit and score threshold values."""
    with pytest.raises(ValidationError):
        DiscoverRequest(paper_ids=[1], limit=0)
    with pytest.raises(ValidationError):
        DiscoverRequest(paper_ids=[1], score_threshold=1.5)


def test_ask_response_isolates_sources_default():
    """AskResponse instances should not share their source lists."""
    first = AskResponse(answer="one")
    second = AskResponse(answer="two")
    first.sources.append(AskSourceItem(content="source"))

    assert second.sources == []


def test_topic_response_supports_from_attributes():
    """TopicResponse validates attribute-backed rows via from_attributes."""
    row = type(
        "TopicRow",
        (),
        {
            "id": 1,
            "name": "Retrieval",
            "query_terms": ["rag", "retrieval"],
            "category": "ml",
            "enabled": True,
            "created_at": datetime.now(UTC),
        },
    )()

    response = TopicResponse.model_validate(row)

    assert response.name == "Retrieval"


def test_citation_graph_response_round_trips_nodes_and_edges():
    """CitationGraphResponse keeps graph nodes and edges typed for the frontend."""
    graph = CitationGraphResponse(
        nodes=[GraphNode(id=1, title="Paper A")],
        edges=[GraphEdge(source=1, target=2, context="mentions")],
    )

    assert graph.nodes[0].display_size == 20
    assert graph.edges[0].context == "mentions"


def test_extraction_field_defaults_to_text_type():
    """ExtractionField defaults to the text type when the template omits one."""
    field = ExtractionField(name="method", label="Method", description="Core method")

    assert field.type == "text"
