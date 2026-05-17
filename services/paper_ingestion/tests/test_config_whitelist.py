"""Tests for config key whitelist and settings validation."""

import pytest
from paper_ingestion.routers.settings import (
    _ALLOWED_CONFIG_KEYS,
    _CONFIG_VALIDATORS,
    SYSTEM_KEYS,
    _classify_config_key,
)

_LANGFUSE_KEY = "observability.langfuse_dashboard_url"

# Keys the frontend renders in IngestionSection.tsx CONFIG_METADATA
_FRONTEND_KEYS = {
    "fsrs.desired_retention",
    "fsrs.learning_steps",
    "llm.smart_model",
    "llm.fast_model",
    "llm.embed_model",
}

# Vestigial notification keys removed in Wave-1 β1 (APScheduler owns scheduled_nudges)
_REMOVED_NOTIFICATION_KEYS = {
    "notifications.timezone",
    "notifications.morning_briefing",
    "notifications.paper_digest",
    "notifications.review_reminder",
}

# New user-preferences key replacing notifications.timezone
_USER_PREF_KEYS = {"user.timezone"}


def test_frontend_metadata_keys_all_allowed():
    """Every key the frontend CONFIG_METADATA renders must be in the backend whitelist."""
    missing = _FRONTEND_KEYS - _ALLOWED_CONFIG_KEYS
    assert not missing, f"Frontend keys not in backend whitelist: {missing}"


def test_removed_notification_keys_rejected():
    """Vestigial notifications.* keys must NOT be in the whitelist (they were removed)."""
    still_present = _REMOVED_NOTIFICATION_KEYS & _ALLOWED_CONFIG_KEYS
    assert not still_present, f"Vestigial notification keys still in whitelist: {still_present}"


@pytest.mark.parametrize("key", sorted(_REMOVED_NOTIFICATION_KEYS))
def test_removed_notification_key_not_in_whitelist(key: str):
    """Each removed notification key is individually verified absent."""
    assert key not in _ALLOWED_CONFIG_KEYS, f"{key!r} should have been removed from whitelist"


def test_user_timezone_allowed():
    """user.timezone must be in the whitelist as the replacement for notifications.timezone."""
    missing = _USER_PREF_KEYS - _ALLOWED_CONFIG_KEYS
    assert not missing, f"User-pref keys not in backend whitelist: {missing}"


def test_unknown_key_not_allowed():
    """Verify a clearly invalid key is not in the whitelist."""
    assert "definitely.not.a.real.key" not in _ALLOWED_CONFIG_KEYS


def test_whitelist_is_frozenset():
    """The whitelist should be immutable to prevent accidental mutation."""
    assert isinstance(_ALLOWED_CONFIG_KEYS, frozenset)


def test_llm_role_keys_subset_of_whitelist():
    """All LLM model role keys used by ROLE_TO_ALIAS must be in the whitelist."""
    from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS

    missing = set(ROLE_TO_ALIAS.keys()) - _ALLOWED_CONFIG_KEYS
    assert not missing, f"ROLE_TO_ALIAS keys not in whitelist: {missing}"


# --- observability.langfuse_dashboard_url (webapp-driven Langfuse link) ---


def test_langfuse_dashboard_url_allowed_and_system_scoped():
    """The Langfuse dashboard URL is a deployment-wide (admin-only) config key."""
    assert _LANGFUSE_KEY in _ALLOWED_CONFIG_KEYS
    assert _LANGFUSE_KEY in SYSTEM_KEYS
    assert _classify_config_key(_LANGFUSE_KEY) == "system"


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty clears the link
        "https://cloud.langfuse.com/project/abc",
        "https://langfuse.example.com",
        "http://localhost:3002",
        "http://127.0.0.1:3002/",
    ],
)
def test_langfuse_dashboard_url_accepts_safe_values(value: str):
    validator = _CONFIG_VALIDATORS[_LANGFUSE_KEY]
    validator(value)  # must not raise


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.example.com",  # plain http to a non-loopback host
        "ftp://localhost:3002",  # non-http(s) scheme
        "not a url",
        "https://",  # scheme without a host
        "javascript:alert(1)",
        3002,  # non-string
        None,
    ],
)
def test_langfuse_dashboard_url_rejects_unsafe_values(value: object):
    validator = _CONFIG_VALIDATORS[_LANGFUSE_KEY]
    with pytest.raises(ValueError):
        validator(value)
