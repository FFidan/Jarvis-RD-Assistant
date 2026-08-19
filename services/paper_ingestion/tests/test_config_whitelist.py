"""Tests for config key whitelist and settings validation."""

import socket

import pytest
from jarvis_common.config_metadata import (
    _ALLOWED_CONFIG_KEYS,
    PERSONAL_KEYS,
    SYSTEM_KEYS,
    _classify_config_key,
)
from jarvis_common.config_validators import _CONFIG_VALIDATORS

_LANGFUSE_KEY = "observability.langfuse_dashboard_url"
_ONBOARDING_DISMISSED_KEY = "onboarding.dismissed"
_CLASSIFIER_OPT_IN_KEY = "pulse.classifier_opt_in"

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
_UI_PREF_VALUES = {
    "ui.appearance": {
        "theme": "dark",
        "accent": "forest",
        "type": "editorial",
        "density": "compact",
    },
    "ui.timer": {
        "workMinutes": 50,
        "shortBreakMinutes": 10,
        "longBreakMinutes": 25,
        "targetCycles": 6,
    },
    "ui.nav_mode": "full",
}
_MALFORMED_UI_PREF_VALUES = {
    "ui.appearance": {
        "accent": "forest",
        "type": "editorial",
        "density": "compact",
    },
    "ui.timer": {
        "workMinutes": "50",
        "shortBreakMinutes": 10,
        "longBreakMinutes": 25,
        "targetCycles": 6,
    },
    "ui.nav_mode": "wide",
}


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


@pytest.mark.parametrize(("key", "value"), _UI_PREF_VALUES.items())
def test_ui_preferences_are_allowed_personal_and_validated(key: str, value: object):
    """Each interface preference passes every registry required by the write path."""
    assert key in _ALLOWED_CONFIG_KEYS
    assert key in PERSONAL_KEYS
    assert key not in SYSTEM_KEYS
    assert _classify_config_key(key) == "personal"
    _CONFIG_VALIDATORS[key](value)


@pytest.mark.parametrize(("key", "value"), _MALFORMED_UI_PREF_VALUES.items())
def test_ui_preference_validators_reject_malformed_values(key: str, value: object):
    """Malformed interface preferences cannot reach persistence."""
    with pytest.raises(ValueError):
        _CONFIG_VALIDATORS[key](value)


def test_onboarding_dismissal_is_allowed_and_personal():
    """Tour dismissal must round-trip for each user, not become a system setting."""
    assert _ONBOARDING_DISMISSED_KEY in _ALLOWED_CONFIG_KEYS
    assert _ONBOARDING_DISMISSED_KEY in PERSONAL_KEYS
    assert _ONBOARDING_DISMISSED_KEY not in SYSTEM_KEYS
    assert _classify_config_key(_ONBOARDING_DISMISSED_KEY) == "personal"
    validator = _CONFIG_VALIDATORS[_ONBOARDING_DISMISSED_KEY]
    validator(True)
    with pytest.raises(ValueError):
        validator("true")


def test_classifier_opt_in_is_allowed_personal_and_boolean():
    """Classifier opt-in must be writable by the user who owns the model."""
    assert _CLASSIFIER_OPT_IN_KEY in _ALLOWED_CONFIG_KEYS
    assert _CLASSIFIER_OPT_IN_KEY in PERSONAL_KEYS
    assert _CLASSIFIER_OPT_IN_KEY not in SYSTEM_KEYS
    assert _classify_config_key(_CLASSIFIER_OPT_IN_KEY) == "personal"
    validator = _CONFIG_VALIDATORS[_CLASSIFIER_OPT_IN_KEY]
    validator(True)
    validator(False)
    with pytest.raises(ValueError):
        validator("false")


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
    from jarvis_common.config_validators import _validate_langfuse_dashboard_url

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


from jarvis_common.config_validators import _validate_fsrs_learning_steps  # noqa: E402


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


# --- PI-CFG-01: cloud LLM api keys must be system-scoped (admin-only write) ---

_CLOUD_LLM_API_KEYS = {
    "llm.anthropic.api_key",
    "llm.openai.api_key",
    "llm.google.api_key",
}


@pytest.mark.parametrize("key", sorted(_CLOUD_LLM_API_KEYS))
def test_cloud_llm_api_key_in_system_keys(key: str):
    """Cloud LLM API keys must be in SYSTEM_KEYS (deployment-wide, admin-only write)."""
    assert key in SYSTEM_KEYS, f"{key!r} must be in SYSTEM_KEYS"


@pytest.mark.parametrize("key", sorted(_CLOUD_LLM_API_KEYS))
def test_cloud_llm_api_key_not_in_personal_keys(key: str):
    """Cloud LLM API keys must NOT be in PERSONAL_KEYS — they are read WHERE user_id IS NULL."""
    assert key not in PERSONAL_KEYS, f"{key!r} must NOT be in PERSONAL_KEYS"


@pytest.mark.parametrize("key", sorted(_CLOUD_LLM_API_KEYS))
def test_cloud_llm_api_key_classify_returns_system(key: str):
    """_classify_config_key must return 'system' for all cloud LLM API keys."""
    assert _classify_config_key(key) == "system", (
        f"_classify_config_key({key!r}) returned {_classify_config_key(key)!r}, expected 'system'"
    )


# --- auth.api_key_login_enabled (admin-flippable multi-tenant gate) ---

from jarvis_common.auth import API_KEY_LOGIN_CONFIG_KEY  # noqa: E402


def test_api_key_login_key_allowed_and_system_scoped():
    """The API-key-login toggle is a deployment-wide (admin-only) config key."""
    assert API_KEY_LOGIN_CONFIG_KEY == "auth.api_key_login_enabled"
    assert API_KEY_LOGIN_CONFIG_KEY in _ALLOWED_CONFIG_KEYS
    assert API_KEY_LOGIN_CONFIG_KEY in SYSTEM_KEYS
    assert API_KEY_LOGIN_CONFIG_KEY not in PERSONAL_KEYS
    assert _classify_config_key(API_KEY_LOGIN_CONFIG_KEY) == "system"


# --- LLM provider registry keys ---


def test_provider_registry_keys_are_system_scoped_secret_and_encrypted():
    """Provider credentials are protected; a provider endpoint stays readable.

    Encrypting a base URL stored it as ciphertext with no readable value, so the
    settings page showed the custom endpoint as unset while the runtime still
    dialled it. Only the API key of a provider belongs in these two sets.
    """
    from jarvis_common.config_metadata import _ENCRYPTED_KEYS, _SECRET_KEYS
    from jarvis_common.llm_provider_registry import (
        PROVIDER_API_KEY_CONFIG_KEYS,
        PROVIDER_BASE_URL_CONFIG_KEYS,
        PROVIDER_CONFIG_KEYS,
    )

    assert PROVIDER_BASE_URL_CONFIG_KEYS, "a provider base-URL key must exist to be classified"
    assert PROVIDER_CONFIG_KEYS <= SYSTEM_KEYS
    assert PROVIDER_API_KEY_CONFIG_KEYS <= _SECRET_KEYS
    assert PROVIDER_API_KEY_CONFIG_KEYS <= _ENCRYPTED_KEYS
    assert not (PROVIDER_BASE_URL_CONFIG_KEYS & _SECRET_KEYS)
    assert not (PROVIDER_BASE_URL_CONFIG_KEYS & _ENCRYPTED_KEYS)
    assert all(_classify_config_key(key) == "system" for key in PROVIDER_CONFIG_KEYS)


def test_secret_and_encrypted_config_keys_stay_identical():
    """A key that is masked must also be encrypted at rest.

    ``_resolve_config_value`` used to carry a second masking branch for keys in
    ``_SECRET_KEYS`` alone. The branch was unreachable and was removed, so this
    equality is what now keeps a "secret" key from being served in the clear.
    """
    from jarvis_common.config_metadata import _ENCRYPTED_KEYS, _SECRET_KEYS

    assert _SECRET_KEYS == _ENCRYPTED_KEYS


def test_provider_registry_keys_have_validators():
    """Every provider registry config key must be writable only through a validator."""
    from jarvis_common.llm_provider_registry import PROVIDER_CONFIG_KEYS

    missing = PROVIDER_CONFIG_KEYS - set(_CONFIG_VALIDATORS)
    assert not missing


@pytest.mark.usefixtures("fernet_key")
def test_one_unreadable_secret_does_not_break_the_configuration_listing() -> None:
    """A key that can no longer be decrypted must degrade to a single field.

    Restores and key rotation can leave ciphertext the current key cannot read.
    Raising here took down the whole settings page, including the panel an admin
    would use to re-enter the value. The field must also stay distinguishable
    from an absent one, or a broken credential reads as a missing credential.
    """
    from jarvis_common.config_store import _resolve_config_value

    key = "llm.providers.openrouter.api_key"
    row = {"key": key, "encrypted_value": b"not-decryptable", "value": None}

    resolved = _resolve_config_value(key, row)

    assert resolved is not None
    assert "not-decryptable" not in str(resolved)
    assert _resolve_config_value(key, {"key": key, "encrypted_value": None, "value": None}) is None


@pytest.mark.usefixtures("fernet_key")
def test_provider_base_url_stays_readable_while_its_api_key_is_masked() -> None:
    """A configured endpoint must be shown, and its credential must not be."""
    from jarvis_common.config_store import _resolve_config_value
    from jarvis_common.crypto import encrypt_secret

    endpoint = "https://llm.example.com/v1"
    resolved_url = _resolve_config_value(
        "llm.providers.custom_openai_compatible.base_url",
        {"value": endpoint, "encrypted_value": None},
    )
    resolved_key = _resolve_config_value(
        "llm.providers.custom_openai_compatible.api_key",
        {"value": None, "encrypted_value": encrypt_secret("sk-live-secret").encode("ascii")},
    )

    assert resolved_url == endpoint
    assert resolved_key is not None
    assert "sk-live-secret" not in str(resolved_key)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_startup_migration_makes_a_stored_endpoint_readable_again() -> None:
    """An endpoint an earlier release encrypted must come back as a readable row.

    Those rows hold ciphertext with ``value`` NULL. Without this one-shot pass the
    endpoint would read as never configured on the settings page while the runtime
    kept dialling it, and only re-saving it by hand would agree the two again.
    """
    from unittest.mock import AsyncMock

    from jarvis_common.config_store import migrate_plaintext_secrets
    from jarvis_common.crypto import encrypt_secret
    from jarvis_common.testing import make_pool_and_conn

    key = "llm.providers.custom_openai_compatible.base_url"
    endpoint = "https://llm.example.com/v1"
    pool, conn = make_pool_and_conn(with_transaction=False)
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # no legacy plaintext secret rows to re-encrypt
            [
                {
                    "user_id": None,
                    "key": key,
                    "encrypted_value": encrypt_secret(endpoint).encode("ascii"),
                }
            ],
        ]
    )

    moved = await migrate_plaintext_secrets(pool)

    assert moved == 1
    assert [call.args for call in conn.execute.await_args_list] == [
        ("SELECT platform.upsert_config_v1($1, $2, $3::jsonb, NULL)", None, key, endpoint)
    ]


@pytest.mark.parametrize(
    "value",
    [
        "https://llm.example.com/v1",
        "https://api.openai.com/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "https://127.0.0.1",
    ],
)
def test_custom_openai_base_url_accepts_safe_values(value: str):
    """Custom endpoints must accept HTTPS, public hosts, and loopback endpoints."""
    _CONFIG_VALIDATORS["llm.providers.custom_openai_compatible.base_url"](value)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://llm.example.com/v1",
        "https://user:pass@example.com/v1",
        "https://llm.example.com/v1#frag",
        "http://192.168.1.20:8000/v1",
        "http://169.254.169.254/v1",
        # RFC1918 / CGNAT / ULA literal IPs must be blocked even over HTTPS,
        # where the loopback-only HTTP rule would not otherwise fire.
        "http://10.0.0.5:8000",
        "https://192.168.1.1",
        "https://172.16.0.1",
        "https://100.64.0.1",
        "https://[fc00::1]",
        "not a url",
        "",
    ],
)
def test_custom_openai_base_url_rejects_unsafe_values(value: str):
    """Custom endpoints must reject unsafe schemes, credentials, fragments, and addresses."""
    with pytest.raises(ValueError):
        _CONFIG_VALIDATORS["llm.providers.custom_openai_compatible.base_url"](value)


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("10.0.0.5", True),
        ("192.168.1.1", True),
        ("172.16.0.1", True),
        ("100.64.0.1", True),  # CGNAT (RFC 6598)
        ("fc00::1", True),  # ULA (RFC 4193)
        ("127.0.0.1", False),  # loopback dev carve-out
        ("::1", False),  # IPv6 loopback
        ("1.1.1.1", False),  # public
    ],
)
def test_blocked_custom_endpoint_ip_covers_private_ranges(address: str, blocked: bool):
    """Resolved-address guard blocks private/reserved ranges but allows loopback and public."""
    import ipaddress

    from jarvis_common.llm_provider_registry import _blocked_custom_endpoint_ip

    assert _blocked_custom_endpoint_ip(ipaddress.ip_address(address)) is blocked


@pytest.mark.asyncio
async def test_custom_openai_outbound_guard_rejects_link_local_resolution(monkeypatch):
    """Outbound custom endpoints must reject hostnames resolving to link-local IPs."""
    from jarvis_common import llm_provider_registry as registry

    def fake_getaddrinfo(*_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(registry.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="blocked network address"):
        await registry.validate_custom_openai_base_url_for_outbound("https://llm.example.test/v1")


@pytest.mark.asyncio
async def test_custom_openai_outbound_guard_rejects_implicit_loopback(monkeypatch):
    """DNS names resolving to loopback are blocked unless the host is explicit loopback."""
    from jarvis_common import llm_provider_registry as registry

    def fake_getaddrinfo(*_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(registry.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="blocked network address"):
        await registry.validate_custom_openai_base_url_for_outbound("https://llm.example.test/v1")


@pytest.mark.asyncio
async def test_custom_openai_outbound_guard_allows_explicit_loopback():
    """Explicit loopback development endpoints remain valid for custom providers."""
    from jarvis_common.llm_provider_registry import (
        validate_custom_openai_base_url_for_outbound,
    )

    await validate_custom_openai_base_url_for_outbound("http://127.0.0.1:8000/v1")
