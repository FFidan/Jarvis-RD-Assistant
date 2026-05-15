"""Settings, nudges, and source management endpoints."""

import contextlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import dynamic_update
from jarvis_common.auth import current_user_id_strict, require_admin, verify_api_key
from jarvis_common.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    resolve_secret_row,
)
from jarvis_common.event_log import log_event as _log_event
from jarvis_common.settings import get_core_settings, get_telegram_settings
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, get_scheduler, limiter
from paper_ingestion.models import (
    ConfigEntry,
    NudgeResponse,
    NudgeUpdate,
    PapersBySourceItem,
    PapersByStatusItem,
    SourceResponse,
    SourceUpdate,
)
from paper_ingestion.services.litellm_config import (
    ROLE_TO_ALIAS,
    reload_litellm,
    update_litellm_model,
)
from paper_ingestion.services.model_lifecycle import catalog_entry_for_model, normalize_model_tag

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["settings"])


class ProviderTestResponse(BaseModel):
    ok: bool
    error: str | None = None


_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "llm.smart_model",
        "llm.fast_model",
        "llm.embed_model",
        # FSRS
        "fsrs.desired_retention",
        "fsrs.learning_steps",
        # User preferences
        "user.timezone",
        # Recommendation engine
        "recommendation.liked_weight",
        "recommendation.project_weight",
        "recommendation.enabled",
        # Pulse (overnight deck subsystem)
        "pulse.enabled",
        "pulse.cron",
        "pulse.deck_size",
        "pulse.stage2_top_k",
        "pulse.weights",
        "pulse.l2_lambda",
        "pulse.lookback_days",
        "pulse.startup_grace_seconds",
        # Setup wizard
        "setup.completed",
        "telegram.owner_chat_id",
        # Zotero integration
        "zotero.api_key",
        "zotero.user_id",
        "zotero.library_type",
        "zotero.group_id",
        "zotero.poll_enabled",
        "zotero.poll_cron",
        "zotero.auto_push_on_star",
        # Cloud LLM provider keys
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
    }
)

# ---------------------------------------------------------------------------
# Dynamic config key patterns for per-machine hardware-aware settings.
# These cannot be expressed as a literal set because they contain the machine
# hostname as a segment.
#
# Accepted:
#   llm.<hostname>.<role>_num_ctx      e.g. llm.host-rtx5060.smart_num_ctx
#   llm.<hostname>.thinking_disabled.<model_id>
#                                      e.g. llm.host.thinking_disabled.qwen3:14b
#
# <hostname>  = [a-zA-Z0-9.-]{1,64}   (strict; rejects wildcards, slashes, etc.)
# <role>      = smart | fast | embed
# <model_id>  = [a-z0-9_./:-]+        (catalog ID character set)
# ---------------------------------------------------------------------------

_MACHINE_ID_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,64}$")
_ROLE_RE = re.compile(r"^(smart|fast|embed)$")
_MODEL_ID_RE = re.compile(r"^[a-z0-9_./:\-]+$")

# e.g. llm.host-rtx5060.smart_num_ctx
_NUM_CTX_PATTERN = re.compile(r"^llm\.([a-zA-Z0-9.\-]{1,64})\.(smart|fast|embed)_num_ctx$")
# e.g. llm.host-rtx5060.thinking_disabled.qwen3:14b
_THINKING_DISABLED_PATTERN = re.compile(
    r"^llm\.([a-zA-Z0-9.\-]{1,64})\.thinking_disabled\.([a-z0-9_./:\-]+)$"
)


def _is_allowed_config_key(key: str) -> bool:
    """Return True if *key* is either a known static key or a valid dynamic pattern."""
    if key in _ALLOWED_CONFIG_KEYS:
        return True
    if _NUM_CTX_PATTERN.fullmatch(key):
        return True
    if _THINKING_DISABLED_PATTERN.fullmatch(key):
        return True
    return False


# ---------------------------------------------------------------------------
# Key classification: personal vs system
#
# PERSONAL_KEYS — per-user settings.  Any authenticated user (or non-session
# caller in single-tenant mode) may read/write these.  In Wave-3 these will
# be scoped to (user_id, key) rows; today the table has no user_id column, so
# the scoping is enforced at the role level only.
#
# Consumers checked (grounding):
#   zotero.*          → zotero_service._get_zotero_config()  (per-user Zotero API)
#   llm.*.api_key     → _cloud_provider_key_present(), test_provider endpoint
#   fsrs.*            → learning_engine FSRS (per-user retention schedule)
#   user.timezone     → telegram_bot nudge reload
#   recommendation.*  → recommendation engine; NOTE: currently system-wide
#                        (no per-user table), left PERSONAL so users can tune
#                        their own feed weights.
#
# SYSTEM_KEYS — deployment-wide settings.  Require admin role when the
# request carries a browser session; API-key-only callers (Telegram bot,
# cron) are unaffected (single-tenant legacy path).
#
# Consumers checked:
#   llm.smart/fast/embed_model → main.py startup + litellm_config (all users)
#   pulse.*                    → scheduler.py (system-wide overnight run)
#   setup.completed            → setup wizard gate (system-wide)
#   telegram.owner_chat_id     → Telegram pairing (one owner, system-wide)
#   llm.<hostname>.*           → dynamic hardware patterns (system-wide)
#
# ---------------------------------------------------------------------------

PERSONAL_KEYS: frozenset[str] = frozenset(
    {
        # FSRS (per-user spaced repetition schedule)
        "fsrs.desired_retention",
        "fsrs.learning_steps",
        # User locale preference
        "user.timezone",
        # Per-user recommendation feed weights
        "recommendation.liked_weight",
        "recommendation.project_weight",
        "recommendation.enabled",
        # Zotero integration (per-user library credentials)
        "zotero.api_key",
        "zotero.user_id",
        "zotero.library_type",
        "zotero.group_id",
        "zotero.poll_enabled",
        "zotero.poll_cron",
        "zotero.auto_push_on_star",
        # Cloud LLM provider keys (per-user API credentials)
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
    }
)

SYSTEM_KEYS: frozenset[str] = frozenset(
    {
        # System-wide LLM model role assignments (affects all users + LiteLLM)
        "llm.smart_model",
        "llm.fast_model",
        "llm.embed_model",
        # Pulse overnight deck (system-wide scheduler job)
        "pulse.enabled",
        "pulse.cron",
        "pulse.deck_size",
        "pulse.stage2_top_k",
        "pulse.weights",
        "pulse.l2_lambda",
        "pulse.lookback_days",
        "pulse.startup_grace_seconds",
        # Setup wizard gate
        "setup.completed",
        # Telegram owner pairing (single owner, system-wide)
        "telegram.owner_chat_id",
    }
)
# Note: dynamic llm.<hostname>.* patterns are SYSTEM_KEYS (hardware-wide).
# They cannot be listed here as literals since they contain the machine hostname.
# _classify_config_key() handles them via regex match.

_ZOTERO_LIBRARY_SCOPE_KEYS: frozenset[str] = frozenset(
    {"zotero.library_type", "zotero.user_id", "zotero.group_id"}
)


def _classify_config_key(key: str) -> str:
    """Return 'personal', 'system', or 'unknown' for a config key."""
    if key in PERSONAL_KEYS:
        return "personal"
    if key in SYSTEM_KEYS:
        return "system"
    # Dynamic hardware patterns are system-scoped
    if _NUM_CTX_PATTERN.fullmatch(key) or _THINKING_DISABLED_PATTERN.fullmatch(key):
        return "system"
    return "unknown"


_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "zotero.api_key",
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
    }
)

_ENCRYPTED_KEYS: frozenset[str] = frozenset(
    {
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
        "zotero.api_key",
    }
)


async def migrate_plaintext_secrets(db_pool: asyncpg.Pool) -> int:
    """Eagerly re-encrypt any plaintext rows for keys in :data:`_ENCRYPTED_KEYS`.

    Older rows may still hold a plaintext secret in ``user_config.value`` while
    ``encrypted_value`` is NULL — the result of upgrading from a release that
    predated envelope encryption. This helper runs once at service startup and
    rewrites such rows in place: encrypts ``value`` into ``encrypted_value``
    and clears ``value`` so the API never returns plaintext.

    Skips rows that already have ``encrypted_value`` populated (idempotent).
    Returns the number of rows rewritten.
    """
    if not _ENCRYPTED_KEYS:
        return 0
    keys = sorted(_ENCRYPTED_KEYS)
    rewritten = 0
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, key, value FROM user_config "
            "WHERE key = ANY($1::text[]) AND value IS NOT NULL AND encrypted_value IS NULL",
            keys,
        )
        for row in rows:
            value = row["value"]
            # asyncpg JSONB codec auto-decodes — accept str or numeric values.
            if value is None:
                continue
            plaintext = value if isinstance(value, str) else str(value)
            if not plaintext:
                continue
            try:
                ciphertext_bytes = encrypt_secret(plaintext).encode("ascii")
            except Exception:
                logger.warning(
                    "migrate_plaintext_secrets: encrypt failed for key=%s; skipping",
                    row["key"],
                    exc_info=True,
                )
                continue
            await conn.execute(
                "UPDATE user_config SET value = NULL, encrypted_value = $2, updated_at = NOW() "
                "WHERE id = $1",
                row["id"],
                ciphertext_bytes,
            )
            rewritten += 1
    if rewritten:
        logger.info("migrate_plaintext_secrets: re-encrypted %d row(s)", rewritten)
    return rewritten


_NUDGE_ALLOWED_COLUMNS: set[str] = {"cron_expression", "enabled"}
_NUDGE_JSONB_COLUMNS: frozenset[str] = frozenset()

_SOURCE_ALLOWED_COLUMNS: set[str] = {"enabled", "priority", "config", "display_order"}
_SOURCE_JSONB_COLUMNS: frozenset[str] = frozenset({"config"})


# --- Config key validators ---

_PULSE_WEIGHT_KEYS = frozenset(
    {
        "embedding",
        "topic",
        "llm_relevance",
        "llm_novelty",
        "author_bonus",
        "recency",
        "citation_pagerank",
        "citation_count",
        "citation_adamic_adar",
        "classifier",
    }
)
_PULSE_REQUIRED_WEIGHT_KEYS = frozenset(
    {"embedding", "topic", "llm_relevance", "llm_novelty", "author_bonus", "recency"}
)


def _validate_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("pulse.cron must be a string")
    try:
        trigger = CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc
    # Reject sub-hourly schedules — pulse runs are expensive; once per hour is the minimum.
    base = datetime.now()
    t1 = trigger.get_next_fire_time(None, base)
    t2 = trigger.get_next_fire_time(t1, t1)
    if t1 is not None and t2 is not None and (t2 - t1) < timedelta(hours=1):
        raise ValueError("Pulse cron must fire no more than once per hour")


def _validate_pulse_weights(v: Any) -> None:
    if not isinstance(v, dict):
        raise ValueError("pulse.weights must be a dict")
    keys = set(v.keys())
    if not _PULSE_REQUIRED_WEIGHT_KEYS.issubset(keys) or not keys.issubset(_PULSE_WEIGHT_KEYS):
        raise ValueError(
            "pulse.weights must include the core keys and only known optional keys: "
            f"{sorted(_PULSE_WEIGHT_KEYS)}"
        )
    for k, val in v.items():
        if not isinstance(val, int | float) or isinstance(val, bool) or not (0 <= val <= 1):
            raise ValueError(f"pulse.weights.{k} must be a float between 0 and 1")


def _validate_positive_int(v: Any) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError("value must be a positive integer")


def _validate_bool(v: Any) -> None:
    if not isinstance(v, bool):
        raise ValueError("value must be a boolean")


def _validate_optional_int(v: Any) -> None:
    if v is None:
        return
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("value must be an integer or null")


def _record_get(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _validate_l2_lambda(v: Any) -> None:
    """Validate pulse.l2_lambda — cosine-penalty multiplier for negative signals.

    Range [0, 2]: 0 disables the penalty, 1 = equal-weight, 2 = double-weight.
    Values >2 make the penalty dominate scoring, which is considered unsafe.
    """
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError("pulse.l2_lambda must be a number")
    if not (0.0 <= float(v) <= 2.0):
        raise ValueError("pulse.l2_lambda must be between 0.0 and 2.0")


def _validate_lookback_days(v: Any) -> None:
    """Validate pulse.lookback_days — discovery window in days, [1, 90]."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("pulse.lookback_days must be an integer")
    if not (1 <= v <= 90):
        raise ValueError("pulse.lookback_days must be between 1 and 90")


def _validate_startup_grace_seconds(v: Any) -> None:
    """Validate pulse.startup_grace_seconds — warmup pause, [0, 300]."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError("pulse.startup_grace_seconds must be a number")
    if not (0.0 <= float(v) <= 300.0):
        raise ValueError("pulse.startup_grace_seconds must be between 0 and 300")


def _validate_nonempty_str(v: Any) -> None:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("value must be a non-empty string")


def _validate_library_type(v: Any) -> None:
    if v not in ("user", "group"):
        raise ValueError("zotero.library_type must be 'user' or 'group'")


def _validate_group_id(v: Any) -> None:
    """Validate zotero.group_id — positive integer or null.

    ``null`` is allowed so users can clear the field when switching back to
    a personal library.  When ``library_type`` is ``"group"`` the backend
    requires a non-null positive integer, but that cross-field validation is
    enforced by :class:`~paper_ingestion.integrations.zotero_client.ZoteroClient`
    at construction time.
    """
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError("zotero.group_id must be a positive integer or null")


async def _reload_telegram_nudges() -> None:
    """Best-effort POST to telegram_bot /internal/reload-nudges."""
    telegram_url = get_telegram_settings().url_or_none
    if not telegram_url:
        logger.debug("TELEGRAM_BOT_URL empty — skipping nudge reload")
        return
    api_key_secret = get_core_settings().jarvis_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else ""
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{telegram_url}/internal/reload-nudges",
                headers={"X-API-Key": api_key},
                timeout=2.0,
            )


def _validate_fsrs_retention(v: Any) -> None:
    """Validate fsrs.desired_retention — float in (0, 1) exclusive."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError("fsrs.desired_retention must be a number")
    fv = float(v)
    if not (0.0 < fv < 1.0):
        raise ValueError("fsrs.desired_retention must be between 0.0 and 1.0 (exclusive)")


def _validate_fsrs_learning_steps(v: Any) -> None:
    """Validate fsrs.learning_steps — list of exactly 2 positive integers (minutes)."""
    if not isinstance(v, list):
        raise ValueError("fsrs.learning_steps must be a list")
    if len(v) != 2:
        raise ValueError("fsrs.learning_steps must have exactly 2 elements")
    for i, step in enumerate(v):
        if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
            raise ValueError(f"fsrs.learning_steps[{i}] must be a positive integer (minutes)")


def _validate_zotero_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("zotero.poll_cron must be a string")
    try:
        CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


_CONFIG_VALIDATORS: dict[str, Callable[[Any], None]] = {
    # FSRS
    "fsrs.desired_retention": _validate_fsrs_retention,
    "fsrs.learning_steps": _validate_fsrs_learning_steps,
    "pulse.cron": _validate_cron,
    "pulse.weights": _validate_pulse_weights,
    "pulse.deck_size": _validate_positive_int,
    "pulse.stage2_top_k": _validate_positive_int,
    "pulse.l2_lambda": _validate_l2_lambda,
    "pulse.lookback_days": _validate_lookback_days,
    "pulse.startup_grace_seconds": _validate_startup_grace_seconds,
    "pulse.enabled": _validate_bool,
    "setup.completed": _validate_bool,
    "telegram.owner_chat_id": _validate_optional_int,
    # LLM model role assignments
    "llm.smart_model": _validate_nonempty_str,
    "llm.fast_model": _validate_nonempty_str,
    "llm.embed_model": _validate_nonempty_str,
    # Zotero
    "zotero.api_key": _validate_nonempty_str,
    "zotero.user_id": _validate_nonempty_str,
    "zotero.library_type": _validate_library_type,
    "zotero.group_id": _validate_group_id,
    "zotero.poll_enabled": _validate_bool,
    "zotero.poll_cron": _validate_zotero_cron,
    "zotero.auto_push_on_star": _validate_bool,
    # Cloud LLM provider keys
    "llm.anthropic.api_key": _validate_nonempty_str,
    "llm.openai.api_key": _validate_nonempty_str,
    "llm.google.api_key": _validate_nonempty_str,
}


# --- User Config ---


async def _fetch_installed_ollama_names(request: Request) -> set[str]:
    """Return normalized Ollama model names for assignment validation."""
    http = request.app.state.http_client
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    try:
        resp = await http.get(f"{ollama_url}/api/tags", timeout=10.0)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not verify installed Ollama models",
        ) from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Could not verify installed Ollama models")
    data = resp.json()
    return {normalize_model_tag(str(item.get("name", ""))) for item in data.get("models", [])}


async def _cloud_provider_key_present(provider: str, db_pool: asyncpg.Pool) -> bool:
    config_key = f"llm.{provider}.api_key"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
            config_key,
        )
    return bool(
        row is not None
        and (
            _record_get(row, "encrypted_value") is not None or _record_get(row, "value") is not None
        )
    )


async def _validate_model_assignment(
    *,
    request: Request,
    key: str,
    model_id: str,
    db_pool: asyncpg.Pool,
) -> None:
    """Reject model assignments that are not usable in this deployment."""
    role = ROLE_TO_ALIAS.get(key)
    if role is None:
        return
    entry = catalog_entry_for_model(model_id)
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_id!r} is not in the model catalog",
        )
    if not entry.assignable:
        raise HTTPException(
            status_code=422,
            detail="This model is tracked for evaluation but is not assignable yet.",
        )
    if role not in entry.roles:
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_id!r} cannot be assigned to the {role!r} role",
        )
    if entry.provider == "ollama":
        installed_names = await _fetch_installed_ollama_names(request)
        tag = normalize_model_tag(entry.ollama_tag or entry.id)
        if tag not in installed_names:
            raise HTTPException(status_code=422, detail="Model not pulled. Pull it first.")
        return
    if not await _cloud_provider_key_present(entry.provider, db_pool):
        raise HTTPException(
            status_code=422,
            detail=f"Configure the {entry.provider} API key before assigning this model.",
        )


def _resolve_config_value(key: str, row: Any) -> Any:
    """Return the display value for a config row, applying masking / decryption."""
    if key in _ENCRYPTED_KEYS:
        enc = row.get("encrypted_value")
        if enc is not None:
            # Decrypt then mask — never expose plaintext over the API
            plaintext = decrypt_secret(enc.decode("ascii"))
            return mask_secret(plaintext)
        raw = row.get("value")
        if raw is not None:
            # Legacy plaintext row: mask without decrypting
            return mask_secret(str(raw))
        return None
    if key in _SECRET_KEYS:
        raw = row.get("value")
        return "****" if raw is not None else None
    return row.get("value")


def _has_browser_session(request: Request) -> bool:
    return getattr(request.state, "user_role", None) is not None


async def _fetch_effective_config_row(
    conn: Any,
    key: str,
    user_id: int | None,
    *,
    is_admin: bool = False,
) -> asyncpg.Record | None:
    """Return caller-specific personal config, with NULL-row fallback only for admins.

    For personal keys the NULL-row (system default) is only returned when the
    caller is an admin; regular authenticated users see only their own row
    (404 if absent) to prevent system-default leakage (DOM-A-09).
    System/unknown keys always use the NULL-row path regardless of role.
    """
    scope = _classify_config_key(key)
    if scope == "personal" and user_id is not None:
        if is_admin:
            # Admins may see system default as fallback.
            return await conn.fetchrow(
                """SELECT key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE key = $1 AND (user_id = $2 OR user_id IS NULL)
                   ORDER BY user_id IS NULL
                   LIMIT 1""",
                key,
                user_id,
            )
        # Non-admin: only return the caller's own row — no NULL-row fallback.
        return await conn.fetchrow(
            """SELECT key, value, encrypted_value, user_id
               FROM user_config
               WHERE key = $1 AND user_id = $2""",
            key,
            user_id,
        )
    return await conn.fetchrow(
        """SELECT key, value, encrypted_value, user_id
           FROM user_config
           WHERE key = $1 AND user_id IS NULL""",
        key,
    )


async def _write_config_row(
    conn: Any,
    *,
    user_id: int | None,
    key: str,
    value: Any,
    encrypted_value: bytes | None = None,
) -> None:
    if encrypted_value is not None:
        await conn.execute(
            """INSERT INTO user_config (user_id, key, value, encrypted_value)
               VALUES ($1, $2, NULL, $3)
               ON CONFLICT (user_id, key) DO UPDATE
                   SET value = NULL, encrypted_value = $3, updated_at = NOW()""",
            user_id,
            key,
            encrypted_value,
        )
        return
    await conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES ($1, $2, $3::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE
               SET value = $3::jsonb, encrypted_value = NULL, updated_at = NOW()""",
        user_id,
        key,
        value,
    )


@router.get("/config", response_model=list[ConfigEntry])
@limiter.limit("60/minute")
async def list_config(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ConfigEntry]:
    """Return all config entries.

    Browser users only receive personal settings unless they are admins.
    API-key-only callers preserve the legacy single-tenant view.
    """
    caller_user_id = await current_user_id_strict(request)
    browser_session = _has_browser_session(request)
    role = getattr(request.state, "user_role", None)
    personal_keys = sorted(PERSONAL_KEYS)
    async with db_pool.acquire() as conn:
        if browser_session and role != "admin":
            rows = await conn.fetch(
                """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE key = ANY($1::text[])
                     AND (user_id = $2 OR user_id IS NULL)
                   ORDER BY key, user_id IS NULL""",
                personal_keys,
                caller_user_id,
            )
        elif browser_session and caller_user_id is not None:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE user_id IS NULL OR user_id = $1
                   ORDER BY key, user_id IS NULL""",
                caller_user_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE user_id IS NULL
                   ORDER BY key"""
            )
    return [ConfigEntry(key=r["key"], value=_resolve_config_value(r["key"], r)) for r in rows]


@router.get("/config/{key}")
@limiter.limit("60/minute")
async def get_config(
    request: Request,
    key: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ConfigEntry:
    if not _is_allowed_config_key(key):
        raise HTTPException(404, f"Config key '{key}' not found")
    if _classify_config_key(key) == "system" and _has_browser_session(request):
        await require_admin(request)
    caller_user_id = await current_user_id_strict(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        row = await _fetch_effective_config_row(conn, key, caller_user_id, is_admin=is_admin)
    if not row:
        raise HTTPException(404, f"Config key '{key}' not found")
    value = _resolve_config_value(key, row)
    return ConfigEntry(key=row["key"], value=value)


@router.put("/config/{key}")
@limiter.limit("30/minute")
async def set_config(
    request: Request,
    key: str,
    body: ConfigEntry,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    scheduler=Depends(get_scheduler),
) -> ConfigEntry:
    if not _is_allowed_config_key(key):
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")

    # System-scope keys require admin role when a browser session is present.
    # API-key-only callers (Telegram bot, cron, DEV_MODE) are exempt from role
    # enforcement — they run as the implicit single-tenant owner.
    if _classify_config_key(key) == "system":
        await require_admin(request)
    caller_user_id = await current_user_id_strict(request)
    row_user_id = caller_user_id if _classify_config_key(key) == "personal" else None

    # Dynamic-key validators (num_ctx and thinking_disabled patterns).
    # These are checked before the static _CONFIG_VALIDATORS dict lookup since
    # dynamic keys are never present in that dict.
    if _NUM_CTX_PATTERN.fullmatch(key):
        try:
            _validate_positive_int(body.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif _THINKING_DISABLED_PATTERN.fullmatch(key):
        try:
            _validate_bool(body.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    validator = _CONFIG_VALIDATORS.get(key)
    if validator is not None:
        try:
            validator(body.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if key in ROLE_TO_ALIAS:
        await _validate_model_assignment(
            request=request,
            key=key,
            model_id=str(body.value),
            db_pool=db_pool,
        )
    # For pulse.cron / zotero.poll_cron: read the current value before overwriting
    # so we can roll back if the scheduler refresh fails (DOM-A-12).
    old_pulse_cron: str | None = None
    if key == "pulse.cron":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = 'pulse.cron' AND user_id IS NULL"
            )
        if row is not None and isinstance(row["value"], str):
            old_pulse_cron = row["value"]

    old_zotero_poll_cron: str | None = None
    if key == "zotero.poll_cron":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config"
                " WHERE key = 'zotero.poll_cron' AND user_id IS NOT DISTINCT FROM $1",
                row_user_id,
            )
        if row is not None and isinstance(row["value"], str):
            old_zotero_poll_cron = row["value"]

    if key in ROLE_TO_ALIAS:
        from paper_ingestion.services.litellm_config import _config_lock

        try:
            async with _config_lock:
                updated = await update_litellm_model(key, str(body.value), db_pool=db_pool)
                if updated:
                    reloaded = await reload_litellm()
                    if not reloaded:
                        raise RuntimeError("LiteLLM accepted the alias update but reload failed")
        except (ValueError, RuntimeError) as exc:
            # SEC-002: validation, read-only config, or LiteLLM admin API failure.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Pass body.value directly — asyncpg's JSONB codec (registered via init_pg_connection)
    # handles JSON encoding. Wrapping with json.dumps() would double-encode the value,
    # storing e.g. '"0 4 * * *"' instead of '"0 4 * * *"' in JSONB. (WEB-C01)
    if key in _ENCRYPTED_KEYS:
        # Encrypt the secret and store in encrypted_value; clear plaintext value column.
        ciphertext_bytes = encrypt_secret(str(body.value)).encode("ascii")
        async with db_pool.acquire() as conn:
            await _write_config_row(
                conn,
                user_id=row_user_id,
                key=key,
                value=None,
                encrypted_value=ciphertext_bytes,
            )
    else:
        async with db_pool.acquire() as conn:
            await _write_config_row(conn, user_id=row_user_id, key=key, value=body.value)
    if key == "pulse.cron":
        try:
            if scheduler is not None:
                scheduler.reschedule_job(
                    "pulse_overnight",
                    trigger=CronTrigger.from_crontab(body.value),
                )
                logger.info("pulse_overnight rescheduled live (cron=%s)", body.value)

                # Bounds check: next_run_time must be within [now, now+366d].
                # A malformed or adversarial cron could schedule a run in the past
                # or arbitrarily far in the future.
                job = scheduler.get_job("pulse_overnight")
                now = datetime.now(UTC)
                next_run = job.next_run_time if job is not None else None
                if next_run is None or not (now <= next_run <= now + timedelta(days=366)):
                    logger.error(
                        "pulse_overnight reschedule produced invalid next_run_time=%s"
                        " for cron=%s; reverting",
                        next_run,
                        body.value,
                    )
                    # Roll back DB to the old cron value.
                    _rollback_sql = (
                        "INSERT INTO user_config (user_id, key, value)"
                        " VALUES (NULL, 'pulse.cron', $1::jsonb)"
                        " ON CONFLICT (user_id, key) DO UPDATE"
                        " SET value = $1::jsonb, updated_at = NOW()"
                    )
                    async with db_pool.acquire() as conn:
                        if old_pulse_cron is not None:
                            await conn.execute(_rollback_sql, old_pulse_cron)
                        else:
                            await conn.execute(
                                "DELETE FROM user_config "
                                "WHERE key = 'pulse.cron' AND user_id IS NULL"
                            )
                    # Revert the live scheduler trigger.
                    with contextlib.suppress(Exception):
                        if old_pulse_cron is not None:
                            scheduler.reschedule_job(
                                "pulse_overnight",
                                trigger=CronTrigger.from_crontab(old_pulse_cron),
                            )
                    raise HTTPException(
                        status_code=400,
                        detail="Cron expression produced an invalid next run time"
                        " (must be within the next 366 days)",
                    )
        except HTTPException:
            raise
        except Exception:
            # Let scheduler update failures propagate — silencing them hides broken schedules.
            # The only expected benign failure is "job not found yet" during first-boot;
            # callers should handle that by ensuring the job is registered before saving config.
            raise
    if key == "zotero.poll_cron":
        try:
            if scheduler is not None:
                scheduler.reschedule_job(
                    "zotero_library_sync",
                    trigger=CronTrigger.from_crontab(body.value),
                )
                logger.info("zotero_library_sync rescheduled live (cron=%s)", body.value)
        except Exception:
            # Scheduler refresh failed — roll back the DB write so DB and live cron
            # stay consistent (DOM-A-12).
            _zotero_rollback_sql = (
                "INSERT INTO user_config (user_id, key, value)"
                " VALUES ($1, 'zotero.poll_cron', $2::jsonb)"
                " ON CONFLICT (user_id, key) DO UPDATE"
                " SET value = $2::jsonb, updated_at = NOW()"
            )
            async with db_pool.acquire() as conn:
                if old_zotero_poll_cron is not None:
                    await conn.execute(_zotero_rollback_sql, row_user_id, old_zotero_poll_cron)
                else:
                    await conn.execute(
                        "DELETE FROM user_config"
                        " WHERE key = 'zotero.poll_cron'"
                        " AND user_id IS NOT DISTINCT FROM $1",
                        row_user_id,
                    )
            logger.error(
                "zotero_library_sync reschedule failed; DB write rolled back (cron=%s)",
                body.value,
                exc_info=True,
            )
            raise
    if key in _ZOTERO_LIBRARY_SCOPE_KEYS:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_config WHERE user_id IS NOT DISTINCT FROM $1"
                " AND key = 'zotero.last_library_version'",
                row_user_id,
            )
    if key == "user.timezone":
        # Best-effort: notify telegram_bot to reload nudge jobs with the new timezone
        await _reload_telegram_nudges()
    display_value = mask_secret(str(body.value)) if key in _ENCRYPTED_KEYS else body.value
    # Emit a config-change event for audit trail. Best-effort: never block the
    # response if event logging fails (e.g. pool closed during tests).
    try:
        await _log_event(
            pool=db_pool,
            level="info",
            category="config",
            source="settings",
            message="setting_changed",
            context={"key": key, "new_value": str(display_value)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("config event log_event failed (non-fatal)", exc_info=True)
    return ConfigEntry(key=key, value=display_value)


# --- Scheduled Nudges ---


@router.get("/nudges", response_model=list[NudgeResponse], dependencies=[Depends(require_admin)])
@limiter.limit("60/minute")
async def list_nudges(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[NudgeResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM scheduled_nudges ORDER BY id")
    return [NudgeResponse(**dict(r)) for r in rows]


@router.put("/nudges/{nudge_id}", response_model=NudgeResponse)
@limiter.limit("30/minute")
async def update_nudge(
    request: Request,
    nudge_id: int,
    body: NudgeUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> NudgeResponse:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM scheduled_nudges WHERE id = $1", nudge_id)
        if not existing:
            raise HTTPException(404, f"Nudge {nudge_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_NUDGE_ALLOWED_COLUMNS)
        if not updates:
            return NudgeResponse(**dict(existing))

        if "cron_expression" in updates:
            try:
                _validate_zotero_cron(updates["cron_expression"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        row = await dynamic_update(
            conn,
            "scheduled_nudges",
            nudge_id,
            updates,
            _NUDGE_ALLOWED_COLUMNS,
            jsonb_columns=_NUDGE_JSONB_COLUMNS,
        )

    # Best-effort: notify telegram_bot to reload its nudge jobs
    await _reload_telegram_nudges()

    return NudgeResponse(**dict(row))


# --- Paper Sources ---


class ReorderRequest(BaseModel):
    source_types: list[str]


@router.get("/sources", response_model=list[SourceResponse])
@limiter.limit("60/minute")
async def list_sources(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[SourceResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY display_order ASC, id ASC")
    return [SourceResponse(**dict(r)) for r in rows]


@router.patch("/sources/reorder", response_model=list[SourceResponse])
@limiter.limit("10/minute")
async def reorder_sources(
    request: Request,
    body: ReorderRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> list[SourceResponse]:
    """Persist UI drag-and-drop order by assigning display_order = position index."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT source_type FROM paper_sources")
    existing = {r["source_type"] for r in rows}
    missing = set(body.source_types) - existing
    if missing:
        raise HTTPException(400, detail=f"Unknown sources: {sorted(missing)}")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for idx, stype in enumerate(body.source_types, start=1):
                await conn.execute(
                    "UPDATE paper_sources SET display_order = $1 WHERE source_type = $2",
                    idx,
                    stype,
                )
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY display_order ASC, id ASC")
    return [SourceResponse(**dict(r)) for r in rows]


@router.put("/sources/{source_id}", response_model=SourceResponse)
@limiter.limit("30/minute")
async def update_source(
    request: Request,
    source_id: int,
    body: SourceUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> SourceResponse:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM paper_sources WHERE id = $1", source_id)
        if not existing:
            raise HTTPException(404, f"Source {source_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_SOURCE_ALLOWED_COLUMNS)
        if not updates:
            return SourceResponse(**dict(existing))

        row = await dynamic_update(
            conn,
            "paper_sources",
            source_id,
            updates,
            _SOURCE_ALLOWED_COLUMNS,
            jsonb_columns=_SOURCE_JSONB_COLUMNS,
        )
    return SourceResponse(**dict(row))


# --- Analytics ---


@router.get("/analytics/papers-by-source", response_model=list[PapersBySourceItem])
@limiter.limit("60/minute")
async def papers_by_source(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return paper counts grouped by source type."""
    user_id = await current_user_id_strict(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        if user_id is None or is_admin:
            rows = await conn.fetch(
                "SELECT source_type, COUNT(*) AS count"
                " FROM papers GROUP BY source_type ORDER BY count DESC"
            )
        else:
            rows = await conn.fetch(
                """
                SELECT p.source_type, COUNT(*) AS count
                FROM papers p
                JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
                GROUP BY p.source_type
                ORDER BY count DESC
                """,
                user_id,
            )
    return [{"source_type": r["source_type"], "count": r["count"]} for r in rows]


@router.get("/analytics/papers-by-status", response_model=list[PapersByStatusItem])
@limiter.limit("60/minute")
async def papers_by_status(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return paper counts grouped by user-state status."""
    user_id = await current_user_id_strict(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        if user_id is None or is_admin:
            rows = await conn.fetch(
                """
                SELECT COALESCE(pus.state::TEXT, 'inbox') AS status, COUNT(*) AS count
                FROM papers p
                LEFT JOIN paper_user_state pus ON p.id = pus.paper_id
                GROUP BY COALESCE(pus.state::TEXT, 'inbox')
                ORDER BY count DESC
                """
            )
        else:
            rows = await conn.fetch(
                """
                SELECT COALESCE(pus.state::TEXT, 'inbox') AS status, COUNT(*) AS count
                FROM papers p
                JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
                LEFT JOIN paper_user_state pus
                  ON p.id = pus.paper_id AND pus.user_id = $1
                GROUP BY COALESCE(pus.state::TEXT, 'inbox')
                ORDER BY count DESC
                """,
                user_id,
            )
    return [{"status": r["status"], "count": r["count"]} for r in rows]


# --- Cloud LLM Provider Test ---

_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "google"})


@router.post("/providers/{provider}/test", response_model=ProviderTestResponse)
@limiter.limit("5/minute")
async def test_provider(
    request: Request,
    provider: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
) -> ProviderTestResponse:
    """Probe a cloud LLM provider with its stored API key to verify connectivity."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    config_key = f"llm.{provider}.api_key"
    caller_user_id = await current_user_id_strict(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        row = await _fetch_effective_config_row(conn, config_key, caller_user_id, is_admin=is_admin)

    api_key: str | None = None
    if row is not None:
        try:
            api_key = resolve_secret_row(row)
        except Exception:
            api_key = None

    if not api_key:
        return ProviderTestResponse(ok=False, error="no api key configured")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            if provider == "anthropic":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    json={
                        "model": "claude-sonnet-4-5",
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                )
            elif provider == "openai":
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            else:  # google
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                )
    except httpx.HTTPError as exc:
        return ProviderTestResponse(ok=False, error=str(exc)[:200])

    if resp.is_success:
        return ProviderTestResponse(ok=True)
    return ProviderTestResponse(ok=False, error=f"provider returned HTTP {resp.status_code}")
