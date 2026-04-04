"""Tests for config key whitelist and settings validation."""

from app.routers.settings import _ALLOWED_CONFIG_KEYS


# Keys the frontend renders in IngestionSection.tsx CONFIG_METADATA
_FRONTEND_KEYS = {
    "fsrs.desired_retention", "fsrs.learning_steps",
    "llm.smart_model", "llm.fast_model", "llm.embed_model",
}

# Keys seeded in init.sql that the frontend filters as notifications
_NOTIFICATION_KEYS = {
    "notifications.timezone", "notifications.morning_briefing",
    "notifications.paper_digest", "notifications.review_reminder",
}


def test_frontend_metadata_keys_all_allowed():
    """Every key the frontend CONFIG_METADATA renders must be in the backend whitelist."""
    missing = _FRONTEND_KEYS - _ALLOWED_CONFIG_KEYS
    assert not missing, f"Frontend keys not in backend whitelist: {missing}"


def test_notification_keys_all_allowed():
    """Notification config keys from init.sql must be in the backend whitelist."""
    missing = _NOTIFICATION_KEYS - _ALLOWED_CONFIG_KEYS
    assert not missing, f"Notification keys not in backend whitelist: {missing}"


def test_unknown_key_not_allowed():
    """Verify a clearly invalid key is not in the whitelist."""
    assert "definitely.not.a.real.key" not in _ALLOWED_CONFIG_KEYS


def test_whitelist_is_frozenset():
    """The whitelist should be immutable to prevent accidental mutation."""
    assert isinstance(_ALLOWED_CONFIG_KEYS, frozenset)


def test_llm_role_keys_subset_of_whitelist():
    """All LLM model role keys used by ROLE_TO_ALIAS must be in the whitelist."""
    from app.services.litellm_config import ROLE_TO_ALIAS

    missing = set(ROLE_TO_ALIAS.keys()) - _ALLOWED_CONFIG_KEYS
    assert not missing, f"ROLE_TO_ALIAS keys not in whitelist: {missing}"
