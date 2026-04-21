"""Canonical location shim — implementation at paper_ingestion.pdf_processor."""

from paper_ingestion.pdf_processor import (
    ALLOWED_PDF_DOMAINS,
    MAX_PDF_PAGES,
    MAX_PDF_SIZE,
    PDF_STORAGE_PATH,
    SNAPSHOT_DPI,
    SNAPSHOT_STORAGE_PATH,
    PDFProcessor,
    _extract_text_sync,
    _get_marker_models,
    _validate_pdf_url,
    extract_text,
)

__all__ = [
    "ALLOWED_PDF_DOMAINS",
    "MAX_PDF_PAGES",
    "MAX_PDF_SIZE",
    "PDF_STORAGE_PATH",
    "SNAPSHOT_DPI",
    "SNAPSHOT_STORAGE_PATH",
    "PDFProcessor",
    "_extract_text_sync",
    "_get_marker_models",
    "_validate_pdf_url",
    "extract_text",
]
