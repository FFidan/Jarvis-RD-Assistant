"""Shared FastAPI dependencies for the paper ingestion service."""

from __future__ import annotations

import asyncpg
import httpx
from fastapi import Request
from jarvis_common import create_limiter

# Shared rate limiter instance — imported by routers
limiter = create_limiter()


def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db_pool


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_pdf_processor(request: Request):
    return request.app.state.pdf_processor


def get_verifier(request: Request):
    return request.app.state.verifier


def get_embedder(request: Request):
    return request.app.state.embedder
