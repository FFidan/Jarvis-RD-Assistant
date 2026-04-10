"""Paper source plugins. Import all source modules to trigger registration."""

from app.sources import (
    arxiv_source,  # noqa: F401
    local_source,  # noqa: F401
    openalex_source,  # noqa: F401
    pubmed_source,  # noqa: F401
    semantic_scholar_source,  # noqa: F401
)
