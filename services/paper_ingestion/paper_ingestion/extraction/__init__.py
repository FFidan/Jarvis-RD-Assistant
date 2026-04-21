"""Extraction subpackage — structured field extraction, entity extraction, and verification.

Back-compat re-exports: callers using ``from paper_ingestion.extraction import X``
continue to work via this ``__init__.py``.
"""

from paper_ingestion.extraction.core import (
    batch_extract,
    build_extraction_prompt,
    extract_fields_for_paper,
)

__all__ = [
    "batch_extract",
    "build_extraction_prompt",
    "extract_fields_for_paper",
]
