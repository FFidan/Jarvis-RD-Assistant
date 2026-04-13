"""Paper Ingestion Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
system/models, and router registration.  Endpoint logic lives in
``app.routers.*`` modules.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from jarvis_common import (
    RequestIDMiddleware,
    configure_logging,
    generic_exception_handler,
    http_exception_handler,
    init_pg_connection,
    rate_limit_exceeded_handler,
    validate_production_config,
    validation_exception_handler,
    verify_api_key,
)
from jarvis_common.llm_client import get_litellm_config
from qdrant_client import AsyncQdrantClient
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

# Trigger source registration via imports
import app.sources  # noqa: F401

# Re-export dependency helpers so existing tests can `from app.main import get_db_pool`
from app.deps import (  # noqa: F401
    get_db_pool,
    get_http_client,
    get_pdf_processor,
    get_verifier,
    limiter,
)
from app.embedder import Embedder
from app.models import HealthCheckResponse, PaperSourceConfig, SystemModelsResponse
from app.pdf_processor import PDFProcessor
from app.sources.registry import get_source_class
from app.verification import QuoteVerifier

configure_logging("paper_ingestion", log_level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply unapplied SQL migrations from db/migrations/ on startup."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Bound the advisory-lock wait so a crashed holder never stalls startup.
            await conn.execute("SET LOCAL lock_timeout = '60s'")
            try:
                await conn.execute("SELECT pg_advisory_xact_lock(42)")
            except asyncpg.LockNotAvailableError:
                logger.warning(
                    "migration lock contended — another instance is running migrations; skipping"
                )
                return  # Other instance handles migrations; treat as success
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            applied = {
                r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")
            }

            migrations_dir = Path("/app/db/migrations")
            if not migrations_dir.exists():
                # Fallback for local dev
                migrations_dir = Path(__file__).resolve().parents[3] / "db" / "migrations"
            if not migrations_dir.exists():
                logger.warning("Migrations directory not found, skipping migrations")
                return

            for sql_file in sorted(migrations_dir.glob("*.sql")):
                try:
                    version = int(sql_file.name.split("_")[0])
                except (ValueError, IndexError):
                    logger.warning("Skipping non-migration file: %s", sql_file.name)
                    continue
                if version in applied:
                    continue
                logger.info("Applying migration %s: %s", version, sql_file.name)
                sql = sql_file.read_text()
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                logger.info("Migration %s applied successfully", version)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _refresh_telegram_bot_username(db_pool, http_client: httpx.AsyncClient) -> None:
    """Call Telegram ``getMe`` and cache the bot username in ``user_config``.

    No-op if ``TELEGRAM_BOT_TOKEN`` is unset, if the cached entry is fresh
    (<24h old), or if the API call fails. Never raises: the lifespan hook
    must stay resilient to network/token errors.
    """
    import json as _json
    from datetime import datetime, timedelta

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = 'telegram.bot_username'"
            )
    except Exception:
        logger.warning("telegram.bot_username lookup failed", exc_info=True)
        return

    now = datetime.now(UTC)
    if row is not None:
        value = row["value"]
        if isinstance(value, dict):
            set_at_raw = value.get("set_at")
            try:
                if isinstance(set_at_raw, str):
                    set_at = datetime.fromisoformat(set_at_raw.replace("Z", "+00:00"))
                    if set_at.tzinfo is None:
                        set_at = set_at.replace(tzinfo=UTC)
                    if now - set_at < timedelta(hours=24) and value.get("username"):
                        return
            except ValueError:
                pass  # stale or malformed -> refresh

    try:
        resp = await http_client.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=5.0,
        )
    except Exception:
        logger.warning("Telegram getMe request failed", exc_info=True)
        return

    if resp.status_code != 200:
        logger.warning("Telegram getMe returned HTTP %s", resp.status_code)
        return

    try:
        payload = resp.json()
    except Exception:
        logger.warning("Telegram getMe returned non-JSON payload", exc_info=True)
        return

    if not payload.get("ok"):
        logger.warning("Telegram getMe ok=false: %s", payload.get("description"))
        return
    username = payload.get("result", {}).get("username")
    if not isinstance(username, str) or not username:
        logger.warning("Telegram getMe result missing username")
        return

    cache_value = _json.dumps({"username": username, "set_at": now.isoformat()})
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = NOW()""",
                "telegram.bot_username",
                cache_value,
            )
        logger.info("Telegram bot username cached as @%s", username)
    except Exception:
        logger.warning("Failed to persist telegram.bot_username", exc_info=True)


async def _check_pulse_enabled(db_pool) -> bool:
    """Return True if pulse.enabled is set to true in user_config."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key = 'pulse.enabled'")
        if row is None:
            return False
        value = row["value"]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down shared resources.

    Creates asyncpg connection pool, httpx client, Qdrant client,
    and all service objects.  Stored on ``app.state`` and accessed
    via ``Depends()`` in endpoints.
    """
    validate_production_config()

    database_url = os.environ["DATABASE_URL"]
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")

    app.state.db_pool = await asyncpg.create_pool(
        database_url,
        min_size=int(os.environ.get("DB_POOL_MIN", "2")),
        max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        init=init_pg_connection,
    )
    await run_migrations(app.state.db_pool)
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    )
    qdrant_api_key = os.environ.get("QDRANT_API_KEY") or None
    app.state.qdrant_client = AsyncQdrantClient(
        url=qdrant_url, api_key=qdrant_api_key, check_compatibility=False
    )
    app.state.embedder = Embedder(app.state.http_client, app.state.qdrant_client)
    await app.state.embedder.ensure_collection()
    app.state.pdf_processor = PDFProcessor(app.state.http_client, app.state.embedder)
    app.state.verifier = QuoteVerifier()

    # C-8: Initialize source singletons so the rate limiter persists across requests.
    app.state.sources = {}
    for _source_type_val in ["arxiv", "semantic_scholar", "pubmed", "openalex"]:
        try:
            _source_cls = get_source_class(_source_type_val)
            if not _source_cls:
                continue
            async with app.state.db_pool.acquire() as _conn:
                _row = await _conn.fetchrow(
                    "SELECT id, source_type, enabled, config"
                    " FROM paper_sources WHERE source_type = $1",
                    _source_type_val,
                )
            if _row:
                _config = PaperSourceConfig(
                    id=_row["id"],
                    source_type=_row["source_type"],
                    enabled=_row["enabled"],
                    config=_row["config"] or {},
                )
                app.state.sources[_source_type_val] = _source_cls(_config, app.state.http_client)
        except Exception:
            logger.warning(
                "Could not initialize source singleton for %s",
                _source_type_val,
                exc_info=True,
            )

    # Refresh the cached Telegram bot username (used by the setup wizard
    # to build pairing deep-links). Never raises on failure.
    await _refresh_telegram_bot_username(app.state.db_pool, app.state.http_client)

    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = os.environ.get("JARVIS_API_KEY", "")
    if api_key:
        logger.info("API key authentication enabled")
    elif dev_mode:
        logger.info("DEV_MODE enabled -- running without authentication")
    else:
        logger.warning(
            "JARVIS_API_KEY not set and DEV_MODE not enabled -- service will reject requests"
        )

    _interval = float(os.environ.get("AUTO_FETCH_INTERVAL_HOURS", "0"))
    _pulse_enabled = await _check_pulse_enabled(app.state.db_pool)
    if _interval > 0 or _pulse_enabled:
        from .scheduler import start_scheduler

        app.state.scheduler = await start_scheduler(app, interval_hours=_interval)

    logger.info("Paper Ingestion Service started")
    yield

    if hasattr(app.state, "scheduler"):
        app.state.scheduler.shutdown(wait=False)

    await app.state.qdrant_client.close()
    await app.state.http_client.aclose()
    await app.state.db_pool.close()
    logger.info("Paper Ingestion Service stopped")


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

app = FastAPI(
    title="JARVIS Paper Ingestion",
    description="Paper fetching, PDF processing, and embedding service",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_api_key)],
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)
app.add_middleware(RequestIDMiddleware)

# Standardized error handlers
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

from app.routers import (  # noqa: E402
    analyze,
    authors,
    citations,
    dashboard_api,
    extractions,
    knowledge_graph,
    notes,
    papers,
    pdf,
    priority,
    rag,
    recommendations,
    search,
    settings,
    snapshots,
    system,
    telegram,
    topics,
)
from app.routers import pulse as pulse_router  # noqa: E402

app.include_router(topics.router)
app.include_router(settings.router)
app.include_router(snapshots.router)
app.include_router(authors.router)
app.include_router(citations.router)
app.include_router(extractions.router)
app.include_router(knowledge_graph.router)
app.include_router(dashboard_api.router)
app.include_router(analyze.router)
app.include_router(notes.router)
app.include_router(priority.router)
app.include_router(recommendations.router)
app.include_router(search.router)
app.include_router(papers.router)
app.include_router(pdf.router)
app.include_router(rag.router)
app.include_router(pulse_router.router)
app.include_router(telegram.router)
app.include_router(system.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", dependencies=[], response_model=HealthCheckResponse)
async def health_check(request: Request) -> HealthCheckResponse:
    """Return service health status with dependency probes (no auth required).

    Checks PostgreSQL, Qdrant, and LiteLLM connectivity. Returns ``"ok"``
    when all dependencies are reachable, ``"degraded"`` if any check fails.
    """
    checks: dict[str, str] = {}

    # PostgreSQL
    try:
        async with request.app.state.db_pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        checks["postgres"] = "ok"
    except Exception:
        logger.warning("Health check: PostgreSQL unavailable", exc_info=True)
        checks["postgres"] = "unavailable"

    # Qdrant
    try:
        await asyncio.wait_for(request.app.state.qdrant_client.get_collections(), timeout=5.0)
        checks["qdrant"] = "ok"
    except Exception:
        logger.warning("Health check: Qdrant unavailable", exc_info=True)
        checks["qdrant"] = "unavailable"

    # LiteLLM
    try:
        litellm_config = get_litellm_config()
        resp = await asyncio.wait_for(
            request.app.state.http_client.get(f"{litellm_config.base_url}/health/readiness"),
            timeout=5.0,
        )
        checks["litellm"] = "ok" if resp.status_code == 200 else "unavailable"
    except Exception:
        logger.warning("Health check: LiteLLM unavailable", exc_info=True)
        checks["litellm"] = "unavailable"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "service": "paper_ingestion", "checks": checks}


# ---------------------------------------------------------------------------
# GET /api/system/models
# ---------------------------------------------------------------------------


@app.get("/api/system/models", response_model=SystemModelsResponse)
async def get_system_models(request: Request) -> SystemModelsResponse:
    """Return installed Ollama models + hardware info + current assignments."""
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
    http = request.app.state.http_client
    result: dict = {
        "status": "ok",
        "installed": [],
        "hardware": {},
        "current": {},
        "issues": {},
    }

    try:
        async with request.app.state.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM user_config WHERE key LIKE 'llm.%'")
        for r in rows:
            short_key = r["key"].replace("llm.", "")
            val = r["value"]
            # Strip wrapping quotes from JSONB-encoded strings
            if isinstance(val, str):
                val = val.strip('"')
            result["current"][short_key] = val
    except Exception:
        logger.warning("Could not load current model assignments", exc_info=True)
        result["issues"]["current"] = "Could not load current model assignments."

    try:
        resp = await http.get(f"{ollama_url}/api/tags", timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("models", []):
                result["installed"].append(
                    {
                        "name": m.get("name", ""),
                        "size": m.get("size", 0),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                    }
                )
    except Exception:
        logger.warning("Could not load installed Ollama models", exc_info=True)
        result["issues"]["installed"] = "Could not load installed Ollama models."

    try:
        resp = await http.get(f"{ollama_url}/api/ps", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            result["hardware"]["ollama_running"] = len(data.get("models", []))
    except Exception:
        logger.warning("Could not load Ollama runtime status", exc_info=True)
        result["issues"]["runtime"] = "Could not load Ollama runtime status."

    result["status"] = "ok" if not result["issues"] else "degraded"
    return result
