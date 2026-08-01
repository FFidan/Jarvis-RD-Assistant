"""Unit tests for safe_sse_error_message — each branch exercised independently.

Pure-function tests: no mocks, no fixtures, no DB.
Verified: analyze.py:28-38 safe_sse_error_message at branch boundaries.
"""

from fastapi import HTTPException
from jarvis_common.jobs import JobError

from paper_ingestion.routers.analyze import safe_sse_error_message


def test_http_exception_returns_detail_string():
    exc = HTTPException(status_code=404, detail="Not found here")
    assert safe_sse_error_message(exc) == "Not found here"


def test_job_error_returns_message():
    exc = JobError("Job processing failed: quota exceeded")
    assert safe_sse_error_message(exc) == "Job processing failed: quota exceeded"


def test_value_error_returns_message():
    exc = ValueError("Invalid input supplied")
    assert safe_sse_error_message(exc) == "Invalid input supplied"


def test_generic_exception_returns_safe_fallback():
    exc = RuntimeError("internal stack trace details")
    assert safe_sse_error_message(exc) == "Analysis failed. Please try again."


def test_user_facing_pdf_error_keeps_its_remediation():
    """The stream must not drop a message written for the person watching it."""
    from paper_ingestion.services.pdf_workflow import PDFUserFacingError

    exc = PDFUserFacingError(
        "PDF text-extraction GPU error. Lower OLLAMA_MAX_LOADED_MODELS or set TORCH_DEVICE=cpu."
    )
    assert safe_sse_error_message(exc) == str(exc)
