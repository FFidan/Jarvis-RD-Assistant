"""Config key allow-list, dynamic patterns, and key classification logic."""

import re

from jarvis_common.auth import API_KEY_LOGIN_CONFIG_KEY

from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS
from paper_ingestion.services.llm_provider_registry import (
    CLOUD_MODEL_PREFIXES as _CLOUD_MODEL_PREFIXES,
)
from paper_ingestion.services.llm_provider_registry import (
    CLOUD_PROVIDERS,
    PROVIDER_CONFIG_KEYS,
)

__all__ = [
    "API_KEY_LOGIN_CONFIG_KEY",
    "_ALLOWED_CONFIG_KEYS",
    "PERSONAL_KEYS",
    "SYSTEM_KEYS",
    "_ZOTERO_LIBRARY_SCOPE_KEYS",
    "_SECRET_KEYS",
    "_ENCRYPTED_KEYS",
    "_NUDGE_ALLOWED_COLUMNS",
    "_NUDGE_JSONB_COLUMNS",
    "_SOURCE_ALLOWED_COLUMNS",
    "_SOURCE_JSONB_COLUMNS",
    "_MACHINE_ID_RE",
    "_ROLE_RE",
    "_MODEL_ID_RE",
    "_NUM_CTX_PATTERN",
    "_THINKING_DISABLED_PATTERN",
    "_CLOUD_MODEL_PREFIXES",
    "CLOUD_PROVIDERS",
    "_classify_litellm_runtime_key",
    "_is_cloud_model_assignment",
    "_is_allowed_config_key",
    "_classify_config_key",
]

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
        # Multi-tenant API-key-login gate (admin-flippable recovery toggle;
        # read as env default OR this DB override by jarvis_common.auth).
        API_KEY_LOGIN_CONFIG_KEY,
        "telegram.owner_chat_id",
        # Zotero integration
        "zotero.api_key",
        "zotero.user_id",
        "zotero.library_type",
        "zotero.group_id",
        "zotero.poll_enabled",
        "zotero.poll_cron",
        "zotero.auto_push_on_star",
        # Deployment-wide: which private hosts the Better BibTeX client may reach.
        "zotero.allowed_private_hosts",
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
        "smtp.reply_to",
        "smtp.from_name",
        # Observability — deployment-wide Langfuse dashboard link (admin-only).
        "observability.langfuse_dashboard_url",
        # Automation — auto-fetch pipeline interval (system-wide scheduler).
        "automation.fetch_interval_hours",
        # Automation — opt-in auto-summarize for newly-discovered papers.
        "automation.auto_summarize_discovered",
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


def _classify_litellm_runtime_key(key: str) -> dict[str, str] | None:
    """Classify model-role and per-machine LiteLLM runtime settings."""
    if key in ROLE_TO_ALIAS:
        return {"kind": "model_role", "role_key": key}

    num_ctx_match = _NUM_CTX_PATTERN.fullmatch(key)
    if num_ctx_match:
        machine_id, role = num_ctx_match.groups()
        return {
            "kind": "num_ctx",
            "machine_id": machine_id,
            "role": role,
            "role_key": f"llm.{role}_model",
        }

    thinking_match = _THINKING_DISABLED_PATTERN.fullmatch(key)
    if thinking_match:
        machine_id, model_id = thinking_match.groups()
        return {
            "kind": "thinking_disabled",
            "machine_id": machine_id,
            "model_id": model_id,
        }

    return None


def _is_cloud_model_assignment(model_id: str) -> bool:
    """Return True for LiteLLM cloud-provider model IDs."""
    return model_id.startswith(_CLOUD_MODEL_PREFIXES)


def _is_allowed_config_key(key: str) -> bool:
    """Return True if *key* is either a known static key or a valid dynamic pattern."""
    if key in _ALLOWED_CONFIG_KEYS:
        return True
    if key in PROVIDER_CONFIG_KEYS:
        return True
    return _classify_litellm_runtime_key(key) is not None


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
    }
)

SYSTEM_KEYS: frozenset[str] = (
    frozenset(
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
            # Multi-tenant API-key-login gate — deployment-wide, admin-only write;
            # read WHERE user_id IS NULL by jarvis_common.auth.api_key_login_enabled,
            # so write-scope must match that read-scope.
            API_KEY_LOGIN_CONFIG_KEY,
            # Telegram owner pairing (single owner, system-wide)
            "telegram.owner_chat_id",
            # SMTP relay — one deployment-wide mail config; admin-only.
            "smtp.host",
            "smtp.port",
            "smtp.user",
            "smtp.from",
            "smtp.pass",
            "smtp.reply_to",
            "smtp.from_name",
            # Zotero — which private hosts Better BibTeX may be reached on; the
            # network topology is a property of the deployment, not of a user.
            "zotero.allowed_private_hosts",
            # Observability — one deployment-wide Langfuse dashboard link; admin-only.
            "observability.langfuse_dashboard_url",
            # Automation — pipeline interval; system-wide, admin-only.
            "automation.fetch_interval_hours",
            # Automation — auto-summarize toggle; system-wide, admin-only.
            "automation.auto_summarize_discovered",
        }
    )
    | PROVIDER_CONFIG_KEYS
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


_SECRET_KEYS: frozenset[str] = (
    frozenset(
        {
            "zotero.api_key",
            "smtp.pass",
            "telegram.bot_token",
        }
    )
    | PROVIDER_CONFIG_KEYS
)

_ENCRYPTED_KEYS: frozenset[str] = (
    frozenset(
        {
            "llm.anthropic.api_key",
            "llm.openai.api_key",
            "llm.google.api_key",
            "zotero.api_key",
            # setup.py persists smtp.pass as Fernet ciphertext in encrypted_value;
            # keep the generic /api/config surface masking it consistently.
            "smtp.pass",
            # Telegram bot token stored via setup flow — must be encrypted at rest.
            "telegram.bot_token",
        }
    )
    | PROVIDER_CONFIG_KEYS
)

# ---------------------------------------------------------------------------
# DML constants
# ---------------------------------------------------------------------------

_NUDGE_ALLOWED_COLUMNS: set[str] = {"cron_expression", "enabled"}
_NUDGE_JSONB_COLUMNS: frozenset[str] = frozenset()

_SOURCE_ALLOWED_COLUMNS: set[str] = {"enabled", "priority", "config", "display_order"}
_SOURCE_JSONB_COLUMNS: frozenset[str] = frozenset({"config"})
