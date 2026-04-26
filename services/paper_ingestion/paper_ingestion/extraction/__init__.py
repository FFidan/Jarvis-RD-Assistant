"""Extraction subpackage — structured field extraction, entity extraction, and verification.

Back-compat re-exports: callers using ``from paper_ingestion.extraction import X``
continue to work via this ``__init__.py``.
"""

from paper_ingestion.extraction.core import (
    batch_extract,
    build_extraction_prompt,
    extract_fields_for_paper,
)
from paper_ingestion.extraction.entities import (
    KG_COLLECTION,
    SIMILARITY_THRESHOLD,
    ConnLike,
    build_entity_prompt,
    extract_entities_for_paper,
    get_knowledge_graph,
    query_knowledge_graph,
)
from paper_ingestion.extraction.jobs import (
    _extraction_batch_job,
    _extraction_single_job,
)
from paper_ingestion.extraction.verify import (
    FUZZY_THRESHOLD,
    QuoteVerifier,
)

__all__ = [
    "FUZZY_THRESHOLD",
    "KG_COLLECTION",
    "SIMILARITY_THRESHOLD",
    "ConnLike",
    "QuoteVerifier",
    "_extraction_batch_job",
    "_extraction_single_job",
    "batch_extract",
    "build_entity_prompt",
    "build_extraction_prompt",
    "extract_entities_for_paper",
    "extract_fields_for_paper",
    "get_knowledge_graph",
    "query_knowledge_graph",
]
