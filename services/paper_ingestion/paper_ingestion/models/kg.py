"""Knowledge-graph and citation-graph Pydantic models."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

# --- Citation Graph Models ---


class CitationRelation(BaseModel):
    """A citation relationship between two papers."""

    source_paper_id: int
    cited_paper_id: int
    citation_context: str | None = None
    is_influential: bool | None = None
    intent: list[str] = Field(default_factory=list)


class CitationFetchResponse(BaseModel):
    """Response after fetching citations for a paper."""

    citations_added: int
    references_added: int
    stubs_created: int


class GraphNode(BaseModel):
    """A node in the citation graph."""

    id: int
    title: str
    citation_count: int = 0
    published_date: date | None = None
    is_stub: bool = False
    display_size: int = 20


class GraphEdge(BaseModel):
    """An edge in the citation graph."""

    source: int
    target: int
    is_influential: bool | None = None
    context: str | None = None


class CitationGraphResponse(BaseModel):
    """Full citation graph with nodes and edges."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]


class BatchCitationFetchResponse(BaseModel):
    """Response for POST /api/citations/batch-fetch."""

    queued: int
    message: str


# --- Knowledge Graph Models ---


class EntityCreate(BaseModel):
    """Request body for entity data from LLM extraction."""

    name: str
    entity_type: str
    description: str | None = None


class EntityResponse(BaseModel):
    """Response for an entity."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    canonical_name: str
    entity_type: str
    description: str | None = None
    metadata: dict = Field(default_factory=dict)
    paper_count: int = 1
    created_at: datetime | None = None
    display_size: int = 20


class RelationshipCreate(BaseModel):
    """Relationship data from LLM extraction."""

    source_entity: str  # entity name
    target_entity: str  # entity name
    relationship_type: str
    evidence_quote: str | None = None
    confidence: float = 1.0


class RelationshipResponse(BaseModel):
    """Response for an entity relationship."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_entity_id: int
    target_entity_id: int
    relationship_type: str
    paper_id: int | None = None
    page_number: int | None = None
    evidence_quote: str | None = None
    confidence: float = 1.0
    created_at: datetime | None = None


class EntityExtractionResponse(BaseModel):
    """Response after extracting entities from a paper."""

    entities_added: int
    relationships_added: int
    entities_merged: int
    dropped_relationships: int = 0
    saved_by_full_text_verify: int = 0
    """Number of relationships whose evidence was only found beyond the LLM
    context window cap and saved by the full-text verifier path."""


class KnowledgeGraphResponse(BaseModel):
    """Full knowledge graph data."""

    entities: list[EntityResponse]
    relationships: list[RelationshipResponse]
    entity_type_counts: dict[str, int] = Field(default_factory=dict)


class EntityDetailResponse(BaseModel):
    """Entity with its relationships and papers."""

    entity: EntityResponse
    relationships: list[RelationshipResponse]
    papers: list[dict] = Field(default_factory=list)


class KGQueryResponse(BaseModel):
    """Response for knowledge graph queries."""

    results: list[dict] = Field(default_factory=list)
    query: str
