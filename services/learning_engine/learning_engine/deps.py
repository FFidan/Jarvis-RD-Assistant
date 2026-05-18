"""Shared FastAPI dependencies for the learning engine service."""

import asyncpg
import httpx
from fastapi import Request
from jarvis_common import create_limiter

from learning_engine.anki_exporter import AnkiExporter
from learning_engine.card_generator import CardGenerator
from learning_engine.fsrs_manager import FSRSManager

# Shared rate limiter instance — imported by routers
limiter = create_limiter()


def get_db_pool(request: Request) -> asyncpg.Pool:
    """Return the asyncpg connection pool from app state.

    Parameters
    ----------
    request : Request
        FastAPI request carrying ``app.state.db_pool``.

    Returns
    -------
    asyncpg.Pool
        Shared connection pool created during lifespan startup.
    """
    return request.app.state.db_pool


def get_http_client(request: Request) -> httpx.AsyncClient:
    """Return the shared httpx async client from app state.

    Parameters
    ----------
    request : Request
        FastAPI request carrying ``app.state.http_client``.

    Returns
    -------
    httpx.AsyncClient
        Long-lived client created during lifespan startup.
    """
    return request.app.state.http_client


def get_fsrs_manager(request: Request) -> FSRSManager:
    """Return the default FSRSManager from app state.

    Parameters
    ----------
    request : Request
        FastAPI request carrying ``app.state.fsrs_manager``.

    Returns
    -------
    FSRSManager
        Default manager used for card-creation paths.  Review paths build a
        fresh per-request manager from live DB config via
        ``_build_fsrs_manager_from_db``.
    """
    return request.app.state.fsrs_manager


def get_card_generator(request: Request) -> CardGenerator:
    """Return the CardGenerator from app state.

    Parameters
    ----------
    request : Request
        FastAPI request carrying ``app.state.card_generator``.

    Returns
    -------
    CardGenerator
        LLM-backed card generator initialised during lifespan startup.
    """
    return request.app.state.card_generator


def get_anki_exporter(request: Request) -> AnkiExporter:
    """Return the AnkiExporter from app state.

    Parameters
    ----------
    request : Request
        FastAPI request carrying ``app.state.anki_exporter``.

    Returns
    -------
    AnkiExporter
        Exporter that converts decks to Anki ``.apkg`` format.
    """
    return request.app.state.anki_exporter
