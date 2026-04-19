"""Pulse candidate discovery — fan out to all enabled sources and dedupe.

Queries ``paper_sources`` for enabled source rows, instantiates each via the
source registry, fans out ``fetch_new_since()`` in parallel with
``asyncio.gather(return_exceptions=True)``, and returns a deduplicated list of
``PaperCreate`` candidates ordered by first occurrence.

Dedupe key precedence:
    1. ``metadata["doi"]`` (exact)
    2. ``metadata["arxiv_id"]`` (exact)
    3. SHA1 hash of the lowercased, whitespace-stripped title (first 16 hex)

Failures from individual sources are logged and skipped — Pulse must degrade
gracefully (no source is ever "critical").
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from datetime import datetime
from typing import Any

import httpx

from app.models import PaperCreate, PaperSourceConfig
from app.pulse.profile import UserProfile
from app.sources.base import PaperSource
from app.sources.registry import get_source_class

logger = logging.getLogger(__name__)


def _title_hash(title: str) -> str:
    normalized = " ".join(title.lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _dedupe_key(paper: PaperCreate) -> tuple[str, str]:
    """Return ('kind', value) — the first matching dedupe key.

    We return a tuple so that a DOI "10.1/x" cannot collide with an arxiv_id
    "10.1/x" (highly unlikely, but cheap to enforce).
    """
    meta = paper.metadata or {}
    doi = meta.get("doi")
    if doi:
        return ("doi", str(doi).lower())
    arxiv_id = meta.get("arxiv_id")
    if arxiv_id:
        return ("arxiv", str(arxiv_id).lower())
    return ("title", _title_hash(paper.title))


async def discover_candidates(
    db_pool: Any,
    http_client: httpx.AsyncClient,
    profile: UserProfile,
    since: datetime,
    source_cache: dict | None = None,
) -> list[PaperCreate]:
    """Fan out to all enabled sources and return a deduplicated candidate list.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool used to fetch ``paper_sources``.
    http_client:
        Shared httpx client passed through to every source instance.
    profile:
        ``UserProfile`` (topics + ``stage2_top_k`` used to size the per-source cap).
    since:
        Lower bound on publication/submission date — sources translate this to
        their native filter (e.g. arXiv ``submittedDate``).
    source_cache:
        Optional dict mapping source_type string → singleton ``PaperSource``
        instance (e.g. ``app.state.sources``).  When provided, cached instances
        are preferred so rate-limiter state persists across Pulse runs.  A new
        instance is created only for source types absent from the cache.

    Returns
    -------
    list[PaperCreate]
        Deduplicated candidates, first-occurrence wins.  Returns ``[]`` if no
        sources are enabled or if every source fails.
    """
    async with db_pool.acquire() as conn:
        source_rows = await conn.fetch(
            "SELECT id, source_type, enabled, config FROM paper_sources"
            " WHERE enabled = TRUE ORDER BY display_order ASC, id ASC"
        )

    if not source_rows:
        return []

    sources: list[PaperSource] = []
    for row in source_rows:
        source_type = row["source_type"]
        # Use cached singleton when available to preserve rate-limiter state.
        if source_cache and source_type in source_cache:
            sources.append(source_cache[source_type])
            continue
        cls = get_source_class(source_type)
        if cls is None:
            logger.warning("pulse.discover: no registered class for source_type=%s", source_type)
            continue
        config = PaperSourceConfig(
            id=row["id"],
            source_type=source_type,
            enabled=row["enabled"],
            config=row["config"] or {},
        )
        try:
            sources.append(cls(config=config, http_client=http_client))
        except Exception:
            logger.exception("pulse.discover: failed to instantiate source %s", source_type)

    if not sources:
        return []

    per_source_cap = max(
        10,
        min(
            profile.stage2_top_k,
            math.ceil(profile.stage2_top_k * 2 / max(1, len(sources))),
        ),
    )

    logger.info(
        "pulse.discover: fan-out start sources=%d per_source_cap=%d",
        len(sources),
        per_source_cap,
    )

    results = await asyncio.gather(
        *[src.fetch_new_since(since, profile.topics, limit=per_source_cap) for src in sources],
        return_exceptions=True,
    )

    candidates: list[PaperCreate] = []
    seen: set[tuple[str, str]] = set()
    total_raw = 0
    for src, result in zip(sources, results, strict=False):
        if isinstance(result, BaseException):
            logger.warning(
                "pulse.discover: source %s failed: %s",
                src.__class__.__name__,
                result,
            )
            continue
        total_raw += len(result)
        for paper in result:
            key = _dedupe_key(paper)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(paper)

    logger.info(
        "pulse.discover: collected raw=%d deduped=%d",
        total_raw,
        len(candidates),
    )
    return candidates
