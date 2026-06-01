"""Tests for config key whitelist and settings validation."""

import pytest
from paper_ingestion.services.config_metadata import (
    _ALLOWED_CONFIG_KEYS,
    SYSTEM_KEYS,
    _classify_config_key,
)
from paper_ingestion.services.config_validators import _CONFIG_VALIDATORS

_LANGFUSE_KEY = "observability.langfuse_dashboard_url"

# Keys the frontend renders in IngestionSection.tsx CONFIG_METADATA
_FRONTEND_KEYS = {
    "fsrs.desired_retention",
    "fsrs.learning_steps",
    "llm.smart_model",
    "llm.fast_model",
    "llm.embed_model",
}

# Vestigial notification keys removed (APScheduler owns scheduled_nudges)
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


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://[::1]:3002",
        "http://0.0.0.0:3002",
        "http://127.0.0.2:3002",
        "http://169.254.169.254",
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://192.168.1.1",
    ],
)
def test_validate_langfuse_dashboard_url_rejects_ssrf_boundaries(unsafe_url):
    """SSRF-classic non-loopback HTTP hostnames must be rejected.

    The validator only whitelists `http://localhost` and `http://127.0.0.1`;
    every other HTTP host is treated as a potential SSRF vector to internal
    services or cloud metadata endpoints.
    """
    from paper_ingestion.services.config_validators import _validate_langfuse_dashboard_url

    with pytest.raises(ValueError, match=r"localhost|127\.0\.0\.1|https"):
        _validate_langfuse_dashboard_url(unsafe_url)


# --- smtp.* keys ---

_SMTP_KEYS = {"smtp.host", "smtp.port", "smtp.user", "smtp.from", "smtp.pass"}


@pytest.mark.parametrize("key", sorted(_SMTP_KEYS))
def test_smtp_keys_registered_in_validators(key: str):
    """Every smtp.* config key must have a validator entry."""
    assert key in _CONFIG_VALIDATORS, f"{key!r} missing from _CONFIG_VALIDATORS"


@pytest.mark.parametrize(
    "key,value",
    [
        ("smtp.host", "mail.example.com"),
        ("smtp.port", 587),
        ("smtp.user", "user@example.com"),
        ("smtp.from", "noreply@example.com"),
        ("smtp.pass", "s3cr3t"),
    ],
)
def test_smtp_validators_accept_valid_values(key: str, value: object):
    """Each smtp.* validator must accept a well-formed value without raising."""
    _CONFIG_VALIDATORS[key](value)


def test_smtp_port_rejects_string():
    """smtp.port must reject a string — it must be a positive integer."""
    with pytest.raises(ValueError):
        _CONFIG_VALIDATORS["smtp.port"]("587")


# --- fsrs.learning_steps boundary cases (CFG-RECVAL-1) ---


from paper_ingestion.services.config_validators import _validate_fsrs_learning_steps  # noqa: E402


def test_fsrs_learning_steps_single_step_accepted():
    """A single-element list must be accepted — relaxed from the old 'exactly 2' rule."""
    _validate_fsrs_learning_steps([1])  # must not raise


def test_fsrs_learning_steps_two_steps_still_accepted():
    """The canonical 2-step configuration must continue to be accepted."""
    _validate_fsrs_learning_steps([1, 10])  # must not raise


def test_fsrs_learning_steps_ten_steps_accepted():
    """Upper bound of 10 elements is the maximum allowed."""
    _validate_fsrs_learning_steps(list(range(1, 11)))  # 10 elements — must not raise


def test_fsrs_learning_steps_empty_rejected():
    """An empty list must be rejected with an 'at least 1' error message."""
    with pytest.raises(ValueError, match="at least 1"):
        _validate_fsrs_learning_steps([])


def test_fsrs_learning_steps_oversize_rejected():
    """A list with more than 10 elements must be rejected with an 'at most 10' message."""
    with pytest.raises(ValueError, match="at most 10"):
        _validate_fsrs_learning_steps(list(range(1, 12)))  # 11 elements


def test_fsrs_learning_steps_non_list_rejected():
    """A non-list value must be rejected."""
    with pytest.raises(ValueError, match="must be a list"):
        _validate_fsrs_learning_steps("1,10")


def test_fsrs_learning_steps_zero_element_rejected():
    """Zero is not a valid step duration — must be rejected."""
    with pytest.raises(ValueError):
        _validate_fsrs_learning_steps([0])
