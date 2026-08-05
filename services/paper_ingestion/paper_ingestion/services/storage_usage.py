"""Storage-usage probes behind ``GET /api/system/storage``.

Every backend section degrades to a null/error state on failure rather than
failing the whole snapshot; the router composes these probes into the
``StorageResponse`` payload.
"""

import logging
import shutil
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from pydantic import BaseModel, Field

from paper_ingestion.services.system_models_view import _load_installed_ollama_models

logger = logging.getLogger(__name__)


# Free-space floor for the `pressure` flag: the smallest documented image-pull
# budget (scripts/setup_lib.sh _image_budget_gb "cpu-pull" = 6 GB) — below this,
# even a routine registry pull is likely to fail.
_LOW_DISK_FREE_GB = 6.0

# Mirrors the `hf_cache` volume mount in docker-compose.yml (persists Docling's
# HuggingFace layout/table models across container recreation). This is the
# only writable, app-owned mount available to this zero-privilege container
# (no postgres_data / qdrant_data mount), so it also doubles as the
# disk-pressure probe target below.
_HF_CACHE_DIR = Path("/tmp/hf_cache")


class StorageSection(BaseModel):
    """One storage backend's usage. ``bytes_used`` is None when the size is
    unknown: either the backend was unreachable (``error`` set) or it has no
    byte-level size API (Qdrant — see ``qdrant_collections`` for its proxy).
    """

    bytes_used: int | None = None
    error: str | None = None


class QdrantCollectionUsage(BaseModel):
    """Per-collection point count — the closest usage signal qdrant-client
    1.17.1 exposes; it has no per-collection byte-size API.
    """

    name: str
    points_count: int | None = None


class StorageResponse(BaseModel):
    """Disk-usage snapshot for admins: GET /api/system/storage.

    Every section degrades to a null/empty state on failure rather than
    5xx-ing the whole request — one unreachable backend must never hide the
    usage story for the others.
    """

    ollama_models: StorageSection
    postgres: StorageSection
    qdrant: StorageSection
    qdrant_collections: list[QdrantCollectionUsage] = Field(default_factory=list)
    hf_cache: StorageSection
    pressure: bool


def _dir_size_bytes(path: Path) -> int:
    """Recursive directory byte size (a pure-Python ``du -sb``)."""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


async def _ollama_storage_usage(http: httpx.AsyncClient, ollama_url: str) -> StorageSection:
    installed, issue = await _load_installed_ollama_models(http, ollama_url)
    if issue:
        return StorageSection(error=issue)
    return StorageSection(bytes_used=sum(m.get("size", 0) for m in installed))


async def _postgres_storage_usage(pool: asyncpg.Pool) -> StorageSection:
    try:
        async with pool.acquire() as conn:
            size = await conn.fetchval("SELECT pg_database_size(current_database())")
        return StorageSection(bytes_used=int(size) if size is not None else None)
    except Exception as exc:  # noqa: BLE001 — surface class name, not raw message
        logger.warning("storage: postgres size probe failed", exc_info=True)
        return StorageSection(error=type(exc).__name__)


async def _qdrant_storage_usage(
    qdrant: Any | None,
) -> tuple[StorageSection, list[QdrantCollectionUsage]]:
    """Qdrant exposes no per-collection byte size; point counts are the proxy."""
    if qdrant is None:
        return StorageSection(error="Qdrant client not available"), []
    try:
        collections = await qdrant.get_collections()
        usages = [
            QdrantCollectionUsage(
                name=c.name,
                points_count=(await qdrant.get_collection(c.name)).points_count,
            )
            for c in collections.collections
        ]
        return StorageSection(), usages
    except Exception as exc:  # noqa: BLE001 — surface class name, not raw message
        logger.warning("storage: qdrant probe failed", exc_info=True)
        return StorageSection(error=type(exc).__name__), []


def _hf_cache_storage_usage() -> StorageSection:
    if not _HF_CACHE_DIR.is_dir():
        return StorageSection(bytes_used=0)
    try:
        return StorageSection(bytes_used=_dir_size_bytes(_HF_CACHE_DIR))
    except OSError as exc:
        logger.warning("storage: hf_cache probe failed", exc_info=True)
        return StorageSection(error=type(exc).__name__)


def _disk_pressure() -> bool:
    """True when free space on the hf_cache volume drops below the safe floor."""
    try:
        free_gb = shutil.disk_usage(_HF_CACHE_DIR).free / 1e9
    except OSError:
        return False
    return free_gb < _LOW_DISK_FREE_GB
