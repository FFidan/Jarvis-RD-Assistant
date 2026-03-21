"""Shared FastAPI dependencies for the learning engine service."""

import asyncpg
import httpx
from fastapi import Request
from jarvis_common import create_limiter

from app.anki_exporter import AnkiExporter
from app.card_generator import CardGenerator
from app.fsrs_manager import FSRSManager

# Shared rate limiter instance — imported by routers
limiter = create_limiter()


def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db_pool


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_fsrs_manager(request: Request) -> FSRSManager:
    return request.app.state.fsrs_manager


def get_card_generator(request: Request) -> CardGenerator:
    return request.app.state.card_generator


def get_anki_exporter(request: Request) -> AnkiExporter:
    return request.app.state.anki_exporter
