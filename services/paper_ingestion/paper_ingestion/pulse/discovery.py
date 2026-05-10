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
from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

from paper_ingestion.models import PaperCreate, PaperSourceConfig
from paper_ingestion.pulse.profile import UserProfile
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import get_source_class

logger = logging.getLogger(__name__)

SourceDiagnostics = dict[str, dict[str, Any]]


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


def _retry_after_seconds(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return int(float(retry_after))
    except (TypeError, ValueError):
        return None


def _diagnostic_for_exception(source_name: str, exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return {
            "status": "rate_limit",
            "message": f"{source_name} rate limit reached. Retry later.",
            "status_code": status_code,
            "retry_after_s": _retry_after_seconds(exc),
            "settings_hint": None,
        }
    return {
        "status": "api_error" if status_code else "error",
        "message": (
            f"{source_name} request failed. Check provider status and source configuration."
        ),
        "status_code": status_code,
        "retry_after_s": _retry_after_seconds(exc),
        "settings_hint": None,
    }


def _diagnostic_for_empty_source(src: PaperSource) -> dict[str, Any]:
    source_diagnostic = getattr(src, "last_poll_diagnostic", None)
    if isinstance(source_diagnostic, dict) and source_diagnostic.get("status"):
        return dict(source_diagnostic)
    source_type = getattr(src, "source_type", src.__class__.__name__)
    if getattr(src, "supports_pulse_polling", True) is False:
        return {
            "status": "unsupported",
            "message": f"{src.__class__.__name__} does not support Pulse polling.",
            "status_code": None,
            "retry_after_s": None,
            "settings_hint": None,
        }
    if source_type == "openalex":
        has_openalex_key = bool(getattr(src, "_api_key", None) or getattr(src, "_email", None))
        if not has_openalex_key:
            return {
                "status": "unconfigured",
                "message": (
                    "OpenAlex requires OPENALEX_EMAIL or OPENALEX_API_KEY for Pulse polling."
                ),
                "status_code": None,
                "retry_after_s": None,
                "settings_hint": "Set OPENALEX_EMAIL or OPENALEX_API_KEY, or disable OpenAlex.",
            }
    return {
        "status": "empty",
        "message": f"{src.__class__.__name__} returned no candidates.",
        "status_code": None,
        "retry_after_s": None,
        "settings_hint": None,
    }


async def discover_candidates(
    db_pool: Any,
    http_client: httpx.AsyncClient,
    profile: UserProfile,
    since: datetime,
    source_cache: dict | None = None,
    include_diagnostics: bool = True,
) -> tuple[list[PaperCreate], dict[str, int], SourceDiagnostics]:
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
    include_diagnostics:
        Deprecated parameter; diagnostics are always returned.  Kept for
        backward compatibility; ignored.

    Returns
    -------
    tuple[list[PaperCreate], dict[str, int], SourceDiagnostics]
        A 3-tuple of:
        - Deduplicated candidates, first-occurrence wins.  Returns ``[]`` if no
          sources are enabled or if every source fails.
        - Per-plugin raw fetch counts keyed by source class name.
        - Per-plugin diagnostic dicts keyed by source class name.
    """
    async with db_pool.acquire() as conn:
        source_rows = await conn.fetch(
            "SELECT id, source_type, enabled, config FROM paper_sources"
            " WHERE enabled = TRUE ORDER BY display_order ASC, id ASC"
        )

    if not source_rows:
        return ([], {}, {})

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
        return ([], {}, {})

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

    # ------------------------------------------------------------------
    # Cooldown gate — skip sources that are in a persistent cooldown.
    # ------------------------------------------------------------------
    source_diagnostics: SourceDiagnostics = {}
    ready_sources: list[PaperSource] = []
    for src in sources:
        src_type = getattr(src, "source_type", None)
        if src_type is None:
            ready_sources.append(src)
            continue
        rate_limiter = PersistentSourceRateLimiter(
            source_type=src_type,
            user_id=profile.user_id,
            min_interval_seconds=0,  # gate only checks cooldown_until, not interval
            db_pool=db_pool,
        )
        in_cd, until = await rate_limiter.is_in_cooldown()
        if in_cd:
            plugin_name = src.__class__.__name__
            source_diagnostics[plugin_name] = {
                "status": "cooldown",
                "cooldown_until": until.isoformat() if until else None,
                "message": (f"In cooldown until {until:%H:%M}" if until else "In cooldown"),
                "status_code": None,
                "retry_after_s": None,
                "settings_hint": None,
            }
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO source_run_history"
                    " (user_id, source_type, started_at, finished_at,"
                    " status, candidate_count, duration_ms)"
                    " VALUES ($1, $2, NOW(), NOW(), 'cooldown_skip', 0, 0)",
                    profile.user_id,
                    src_type,
                )
            logger.info(
                "pulse.discover: source %s in cooldown until %s — skipping",
                plugin_name,
                until,
            )
            continue
        ready_sources.append(src)

    results = await asyncio.gather(
        *[
            src.fetch_new_since(
                since, profile.topics, limit=per_source_cap, user_id=profile.user_id
            )
            for src in ready_sources
        ],
        return_exceptions=True,
    )

    candidates: list[PaperCreate] = []
    seen: set[tuple[str, str]] = set()
    source_counts: dict[str, int] = {}
    total_raw = 0
    for src, result in zip(ready_sources, results, strict=False):
        plugin_name = src.__class__.__name__
        if isinstance(result, BaseException):
            logger.warning(
                "pulse.discover: source %s failed: %s",
                plugin_name,
                result,
            )
            source_counts[plugin_name] = 0
            source_diagnostics[plugin_name] = _diagnostic_for_exception(plugin_name, result)
            continue
        source_counts[plugin_name] = len(result)
        source_diagnostics[plugin_name] = (
            {
                "status": "ok",
                "message": f"{plugin_name} returned {len(result)} candidates.",
                "status_code": None,
                "retry_after_s": None,
                "settings_hint": None,
            }
            if result
            else _diagnostic_for_empty_source(src)
        )
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
    return candidates, source_counts, source_diagnostics
