"""Shared FastAPI dependencies for the paper ingestion service."""

from __future__ import annotations

import asyncpg
import httpx
from fastapi import HTTPException, Request
from jarvis_common import create_limiter

# Shared rate limiter instance — imported by routers
limiter = create_limiter()


def get_db_pool(request: Request) -> asyncpg.Pool:
    """Return the shared asyncpg connection pool from app state."""
    return request.app.state.db_pool


def get_http_client(request: Request) -> httpx.AsyncClient:
    """Return the shared httpx async HTTP client from app state."""
    return request.app.state.http_client


def get_pdf_processor(request: Request):
    """Return the PDFProcessor singleton from app state."""
    return request.app.state.pdf_processor


def get_verifier(request: Request):
    """Return the shared ``QuoteVerifier`` from app.state.

    COMPLIANCE-001: routes that need quote verification depend on this rather
    than instantiating ``QuoteVerifier()`` per request -- this keeps a single
    rapidfuzz-cache-warm instance live across requests and lets tests inject
    a fake via ``app.dependency_overrides[get_verifier]``.

    A test-safety fallback lazily instantiates and caches a verifier when
    ``app.state.verifier`` is missing (e.g. when a test bypasses the lifespan
    by importing the FastAPI app directly).  The lazy path is a no-op in
    production because the lifespan hook always sets ``app.state.verifier``
    before the first request can fire.
    """
    verifier = getattr(request.app.state, "verifier", None)
    if verifier is None:
        from jarvis_common.verify import QuoteVerifier  # noqa: PLC0415

        verifier = QuoteVerifier()
        request.app.state.verifier = verifier
    return verifier


def get_embedder(request: Request):
    """Return the Embedder singleton from app state."""
    return request.app.state.embedder


def get_optional_embedder(request: Request):
    """Return embedder if wired on app.state, else None (test-safe)."""
    return getattr(request.app.state, "embedder", None)


def get_optional_qdrant(request: Request):
    """Return Qdrant client if wired on app.state, else None (test-safe)."""
    return getattr(request.app.state, "qdrant_client", None)


def get_s2_source(request: Request):
    """Return the Semantic Scholar source singleton, or raise 503 if missing."""
    s2 = getattr(request.app.state, "s2_source", None)
    if s2 is not None:
        return s2
    sources = getattr(request.app.state, "sources", {})
    s2 = sources.get("semantic_scholar")
    if s2 is not None:
        return s2
    raise HTTPException(status_code=503, detail="Semantic Scholar source not available")


def get_scheduler(request: Request):
    """Return the APScheduler singleton if set on app.state, else None."""
    return getattr(request.app.state, "scheduler", None)
