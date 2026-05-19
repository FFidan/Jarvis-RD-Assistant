"""Business logic extracted from the settings router.

This module contains all non-HTTP concerns that were previously inline in
``paper_ingestion.routers.settings``:

- Config key metadata (allowed list, personal/system/secret classification)
- Validators for each config value type
- DB helpers: fetch effective config row, write config row, resolve display value
- Startup utility: migrate_plaintext_secrets
- Side-effect helpers: reload_telegram_nudges, cloud_provider_key_present
- Model-assignment validation (catalog + Ollama install check)
- Analytics queries: papers-by-source, papers-by-status
- Scheduler helpers: pulse cron reschedule, Zotero cron reschedule, fetch interval reschedule
- Provider connectivity probe (HTTP)
- GDPR data export builder

All functions accept explicit parameters (db_pool, http_client, etc.) rather
than reaching into ``request.app.state`` so they are unit-testable in isolation.
"""

import contextlib
import io
import json
import logging
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import asyncpg
import httpx
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from jarvis_common.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)
from jarvis_common.event_log import (
    log_event as _log_event,  # noqa: F401 — re-exported; live patch site is routers.settings._log_event
)
from jarvis_common.settings import get_core_settings, get_telegram_settings
from pydantic import BaseModel

from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS
from paper_ingestion.services.model_lifecycle import catalog_entry_for_model, normalize_model_tag

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config key allow-list
# ---------------------------------------------------------------------------

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
        # SMTP relay (system-wide; collected in the first-run wizard, viewable
        # and editable post-onboarding by admins). Key names MUST match the
        # rows persisted by routers/setup.py (_SMTP_PLAINTEXT_KEYS / _SMTP_ENCRYPTED_KEYS).
        "smtp.host",
        "smtp.port",
        "smtp.user",
        "smtp.from",
        "smtp.pass",
        # Observability — deployment-wide Langfuse dashboard link (admin-only).
        "observability.langfuse_dashboard_url",
        # Automation — auto-fetch pipeline interval (system-wide scheduler).
        "automation.fetch_interval_hours",
    }
)

# ---------------------------------------------------------------------------
# Dynamic config key patterns for per-machine hardware-aware settings.
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
        # SMTP relay — one deployment-wide mail config; admin-only.
        "smtp.host",
        "smtp.port",
        "smtp.user",
        "smtp.from",
        "smtp.pass",
        # Observability — one deployment-wide Langfuse dashboard link; admin-only.
        "observability.langfuse_dashboard_url",
        # Automation — pipeline interval; system-wide, admin-only.
        "automation.fetch_interval_hours",
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
        "smtp.pass",
    }
)

_ENCRYPTED_KEYS: frozenset[str] = frozenset(
    {
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
        "zotero.api_key",
        # setup.py persists smtp.pass as Fernet ciphertext in encrypted_value;
        # keep the generic /api/config surface masking it consistently.
        "smtp.pass",
    }
)

# ---------------------------------------------------------------------------
# DML constants
# ---------------------------------------------------------------------------

_NUDGE_ALLOWED_COLUMNS: set[str] = {"cron_expression", "enabled"}
_NUDGE_JSONB_COLUMNS: frozenset[str] = frozenset()

_SOURCE_ALLOWED_COLUMNS: set[str] = {"enabled", "priority", "config", "display_order"}
_SOURCE_JSONB_COLUMNS: frozenset[str] = frozenset({"config"})

# ---------------------------------------------------------------------------
# Value validators
# ---------------------------------------------------------------------------

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


def _validate_zotero_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("zotero.poll_cron must be a string")
    try:
        CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


def _validate_langfuse_dashboard_url(v: Any) -> None:
    """Validate observability.langfuse_dashboard_url.

    Accepts an empty string (clears the link), any ``https://`` URL with a
    host, or an ``http://`` URL whose host is loopback (``localhost`` /
    ``127.0.0.1``) so a local-dev Langfuse reachable only over plain HTTP
    still works. Everything else is rejected — the value is rendered as a
    user-facing link, so non-http(s) schemes (e.g. ``javascript:``) and
    plain-HTTP non-loopback hosts must not be storable.
    """
    if not isinstance(v, str):
        raise ValueError("observability.langfuse_dashboard_url must be a string")
    if v == "":
        return
    parsed = urlparse(v)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        return
    raise ValueError(
        "observability.langfuse_dashboard_url must be empty, an https:// URL, "
        "or an http://localhost / http://127.0.0.1 URL"
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
    # Observability
    "observability.langfuse_dashboard_url": _validate_langfuse_dashboard_url,
    # Automation
    "automation.fetch_interval_hours": _validate_positive_int,
    # Cloud LLM provider keys
    "llm.anthropic.api_key": _validate_nonempty_str,
    "llm.openai.api_key": _validate_nonempty_str,
    "llm.google.api_key": _validate_nonempty_str,
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Startup migration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Side-effect helpers
# ---------------------------------------------------------------------------


async def reload_telegram_nudges() -> None:
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


async def cloud_provider_key_present(provider: str, db_pool: asyncpg.Pool) -> bool:
    """Return True if an API key for *provider* is stored in user_config."""
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


# ---------------------------------------------------------------------------
# Model assignment validation
# ---------------------------------------------------------------------------


async def fetch_installed_ollama_names(
    http_client: httpx.AsyncClient,
    ollama_url: str,
) -> set[str]:
    """Return normalized Ollama model names for assignment validation.

    Raises ``httpx.HTTPError`` / ``fastapi.HTTPException`` on failure — callers
    must handle both.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        resp = await http_client.get(f"{ollama_url}/api/tags", timeout=10.0)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not verify installed Ollama models",
        ) from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Could not verify installed Ollama models")
    data = resp.json()
    return {normalize_model_tag(str(item.get("name", ""))) for item in data.get("models", [])}


async def validate_model_assignment(
    *,
    http_client: httpx.AsyncClient,
    ollama_url: str,
    key: str,
    model_id: str,
    db_pool: asyncpg.Pool,
) -> None:
    """Reject model assignments that are not usable in this deployment."""
    from fastapi import HTTPException  # noqa: PLC0415

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
        installed_names = await fetch_installed_ollama_names(http_client, ollama_url)
        tag = normalize_model_tag(entry.ollama_tag or entry.id)
        if tag not in installed_names:
            raise HTTPException(status_code=422, detail="Model not pulled. Pull it first.")
        return
    if not await cloud_provider_key_present(entry.provider, db_pool):
        raise HTTPException(
            status_code=422,
            detail=f"Configure the {entry.provider} API key before assigning this model.",
        )


# ---------------------------------------------------------------------------
# Analytics queries
# ---------------------------------------------------------------------------


async def fetch_papers_by_source(
    conn: Any,
    user_id: int | None,
    *,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Return paper counts grouped by source type, scoped to the caller."""
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


async def fetch_papers_by_status(
    conn: Any,
    user_id: int | None,
    *,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Return paper counts grouped by user-state status, scoped to the caller."""
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


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------


async def apply_pulse_cron(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    new_cron: str,
    old_cron: str | None,
) -> None:
    """Reschedule the pulse_overnight job and validate next_run_time.

    Rolls back the DB write if the job produces an invalid next_run_time, then
    raises ``fastapi.HTTPException(400)``.  Any other scheduler exception
    propagates unchanged.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    if scheduler is None:
        return
    scheduler.reschedule_job(
        "pulse_overnight",
        trigger=CronTrigger.from_crontab(new_cron),
    )
    logger.info("pulse_overnight rescheduled live (cron=%s)", new_cron)

    job = scheduler.get_job("pulse_overnight")
    now = datetime.now(UTC)
    next_run = job.next_run_time if job is not None else None
    if next_run is None or not (now <= next_run <= now + timedelta(days=366)):
        logger.error(
            "pulse_overnight reschedule produced invalid next_run_time=%s for cron=%s; reverting",
            next_run,
            new_cron,
        )
        # Roll back DB to the old cron value.
        _rollback_sql = (
            "INSERT INTO user_config (user_id, key, value)"
            " VALUES (NULL, 'pulse.cron', $1::jsonb)"
            " ON CONFLICT (user_id, key) DO UPDATE"
            " SET value = $1::jsonb, updated_at = NOW()"
        )
        async with db_pool.acquire() as conn:
            if old_cron is not None:
                await conn.execute(_rollback_sql, old_cron)
            else:
                await conn.execute(
                    "DELETE FROM user_config WHERE key = 'pulse.cron' AND user_id IS NULL"
                )
        # Revert the live scheduler trigger.
        with contextlib.suppress(Exception):
            if old_cron is not None:
                scheduler.reschedule_job(
                    "pulse_overnight",
                    trigger=CronTrigger.from_crontab(old_cron),
                )
        raise HTTPException(
            status_code=400,
            detail="Cron expression produced an invalid next run time"
            " (must be within the next 366 days)",
        )


async def apply_zotero_cron(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    new_cron: str,
    old_cron: str | None,
    row_user_id: int | None,
) -> None:
    """Reschedule the zotero_library_sync job; roll back DB on failure (DOM-A-12)."""
    if scheduler is None:
        return
    try:
        scheduler.reschedule_job(
            "zotero_library_sync",
            trigger=CronTrigger.from_crontab(new_cron),
        )
        logger.info("zotero_library_sync rescheduled live (cron=%s)", new_cron)
    except Exception:
        _zotero_rollback_sql = (
            "INSERT INTO user_config (user_id, key, value)"
            " VALUES ($1, 'zotero.poll_cron', $2::jsonb)"
            " ON CONFLICT (user_id, key) DO UPDATE"
            " SET value = $2::jsonb, updated_at = NOW()"
        )
        async with db_pool.acquire() as conn:
            if old_cron is not None:
                await conn.execute(_zotero_rollback_sql, row_user_id, old_cron)
            else:
                await conn.execute(
                    "DELETE FROM user_config"
                    " WHERE key = 'zotero.poll_cron'"
                    " AND user_id IS NOT DISTINCT FROM $1",
                    row_user_id,
                )
        logger.error(
            "zotero_library_sync reschedule failed; DB write rolled back (cron=%s)",
            new_cron,
            exc_info=True,
        )
        raise


def apply_fetch_interval(
    *,
    scheduler: Any,
    hours: int,
) -> None:
    """Reschedule the auto_pipeline job to the new interval (best-effort)."""
    if scheduler is None:
        return
    job = scheduler.get_job("auto_pipeline")
    if job is not None:
        try:
            scheduler.reschedule_job(
                "auto_pipeline",
                trigger=IntervalTrigger(hours=hours),
            )
            logger.info("auto_pipeline rescheduled live (interval=%dh)", hours)
        except Exception:
            logger.warning(
                "auto_pipeline reschedule failed (interval=%dh); persisted value still saved",
                hours,
                exc_info=True,
            )
    else:
        logger.warning(
            "auto_pipeline job not found in scheduler; persisted value will take effect on restart"
        )


# ---------------------------------------------------------------------------
# Provider connectivity probe
# ---------------------------------------------------------------------------


class ProviderTestResult(BaseModel):
    ok: bool
    error: str | None = None


_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "google"})


async def test_provider_connectivity(
    provider: str,
    api_key: str,
) -> ProviderTestResult:
    """Probe a cloud LLM provider with *api_key* to verify connectivity.

    Returns a :class:`ProviderTestResult` — never raises.
    """
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
        return ProviderTestResult(ok=False, error=str(exc)[:200])

    if resp.is_success:
        return ProviderTestResult(ok=True)
    return ProviderTestResult(ok=False, error=f"provider returned HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# GDPR data export
# ---------------------------------------------------------------------------

# (table-in-zip name, SQL). Each query is scoped to the calling user via $1.
# Structured data only: no PDF binaries, no embeddings (paper_chunks.embedding
# / vectors are excluded — papers carries metadata + abstract, notes carry the
# user's own annotations). papers is scoped by discovered_by (canonical-corpus
# owner column, mig 072); everything else by user_id.
_EXPORT_QUERIES: tuple[tuple[str, str], ...] = (
    ("papers", "SELECT row_to_json(p) FROM papers p WHERE p.discovered_by = $1"),
    ("paper_notes", "SELECT row_to_json(t) FROM paper_notes t WHERE t.user_id = $1"),
    ("paper_summaries", "SELECT row_to_json(t) FROM paper_summaries t WHERE t.user_id = $1"),
    ("cards", "SELECT row_to_json(t) FROM cards t WHERE t.user_id = $1"),
    ("decks", "SELECT row_to_json(t) FROM decks t WHERE t.user_id = $1"),
    ("review_logs", "SELECT row_to_json(t) FROM review_logs t WHERE t.user_id = $1"),
    ("projects", "SELECT row_to_json(t) FROM projects t WHERE t.user_id = $1"),
    ("tasks", "SELECT row_to_json(t) FROM tasks t WHERE t.user_id = $1"),
    ("milestones", "SELECT row_to_json(t) FROM milestones t WHERE t.user_id = $1"),
    ("journal_entries", "SELECT row_to_json(t) FROM journal_entries t WHERE t.user_id = $1"),
    ("daily_log", "SELECT row_to_json(t) FROM daily_log t WHERE t.user_id = $1"),
    ("user_config", "SELECT row_to_json(t) FROM user_config t WHERE t.user_id = $1"),
)


async def build_export_zip(pool: asyncpg.Pool, user_id: int | None) -> bytes:
    """Build a ZIP of all user data and return the raw bytes.

    Iterates each :data:`_EXPORT_QUERIES` table inside a single read transaction
    so the snapshot is consistent.  Memory stays bounded because each table's
    rows are fetched via an asyncpg cursor.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        async with pool.acquire() as conn, conn.transaction():
            for name, sql in _EXPORT_QUERIES:
                lines: list[str] = []
                async for record in conn.cursor(sql, user_id):
                    value = record[0]
                    if isinstance(value, str):
                        lines.append(value)
                    else:
                        lines.append(json.dumps(value, default=str))
                zf.writestr(f"{name}.jsonl", "\n".join(lines))
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Config write orchestration
# ---------------------------------------------------------------------------


async def write_config(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    http_client: httpx.AsyncClient,
    ollama_url: str,
    key: str,
    value: Any,
    caller_user_id: int | None,
    update_litellm_model_fn: Any = None,
) -> Any:
    """Persist a config value and apply all related side-effects.

    This is the core of what was previously the ``set_config`` handler body.
    Returns the display value (masked if the key is an encrypted secret).

    Raises ``fastapi.HTTPException`` on validation failure, model-assignment
    rejection, or scheduler rollback.

    Parameters
    ----------
    update_litellm_model_fn:
        Optional callable with the same signature as
        ``paper_ingestion.services.litellm_config.update_litellm_model``.
        When provided, this callable is used instead of the default import so
        callers can substitute a monkeypatched version in tests.  Defaults to
        ``paper_ingestion.services.litellm_config.update_litellm_model``.

    Notes
    -----
    - The ``require_admin`` auth gate is NOT called here — the router handles
      that before delegating so the patch path
      ``paper_ingestion.routers.settings.require_admin`` remains stable.
    - ``log_audit`` and ``_log_event`` stay in the router for the same reason.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    # Validate the value
    if _NUM_CTX_PATTERN.fullmatch(key):
        try:
            _validate_positive_int(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif _THINKING_DISABLED_PATTERN.fullmatch(key):
        try:
            _validate_bool(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    validator = _CONFIG_VALIDATORS.get(key)
    if validator is not None:
        try:
            validator(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    row_user_id = caller_user_id if _classify_config_key(key) == "personal" else None

    # Model assignment check
    if key in ROLE_TO_ALIAS:
        await validate_model_assignment(
            http_client=http_client,
            ollama_url=ollama_url,
            key=key,
            model_id=str(value),
            db_pool=db_pool,
        )

    # Read old cron values before overwriting (for rollback)
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

    # LiteLLM update (before DB write so we can abort on failure).
    # The ``update_litellm_model_fn`` parameter lets callers (i.e. the router)
    # pass a monkeypatched reference so test patches remain effective.
    if key in ROLE_TO_ALIAS:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            _config_lock,
            reload_litellm,
            update_litellm_model,
        )

        _update_fn = (
            update_litellm_model_fn if update_litellm_model_fn is not None else update_litellm_model
        )

        try:
            async with _config_lock:
                updated = await _update_fn(key, str(value), db_pool=db_pool)
                if updated:
                    reloaded = await reload_litellm()
                    if not reloaded:
                        raise RuntimeError("LiteLLM accepted the alias update but reload failed")
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # DB write
    if key in _ENCRYPTED_KEYS:
        ciphertext_bytes = encrypt_secret(str(value)).encode("ascii")
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
            await _write_config_row(conn, user_id=row_user_id, key=key, value=value)

    # Scheduler side-effects
    if key == "pulse.cron":
        try:
            await apply_pulse_cron(
                db_pool=db_pool,
                scheduler=scheduler,
                new_cron=value,
                old_cron=old_pulse_cron,
            )
        except HTTPException:
            raise
        except Exception:
            raise

    if key == "zotero.poll_cron":
        await apply_zotero_cron(
            db_pool=db_pool,
            scheduler=scheduler,
            new_cron=value,
            old_cron=old_zotero_poll_cron,
            row_user_id=row_user_id,
        )

    if key == "automation.fetch_interval_hours":
        hours = max(int(value), 1)
        apply_fetch_interval(scheduler=scheduler, hours=hours)

    # Zotero library-scope cache bust
    if key in _ZOTERO_LIBRARY_SCOPE_KEYS:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_config WHERE user_id IS NOT DISTINCT FROM $1"
                " AND key = 'zotero.last_library_version'",
                row_user_id,
            )

    # Telegram nudge reload on timezone change
    if key == "user.timezone":
        await reload_telegram_nudges()

    # Return display value (masked for secrets)
    return mask_secret(str(value)) if key in _ENCRYPTED_KEYS else value
