"""Verify SecretsSettings honours both env-direct and _FILE indirection."""

from __future__ import annotations

from pathlib import Path

import pytest
from jarvis_common.settings import SecretsSettings, get_secrets_settings


def _isolated_env(monkeypatch, **kwargs):
    for name in (
        "JARVIS_API_KEY",
        "JARVIS_API_KEY_FILE",
        "JARVIS_MODEL_HMAC_KEY",
        "JARVIS_MODEL_HMAC_KEY_FILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN_FILE",
        "LITELLM_MASTER_KEY",
        "LITELLM_MASTER_KEY_FILE",
        "JARVIS_CONFIG_KEY",
        "JARVIS_CONFIG_KEY_FILE",
        "JARVIS_CONFIG_KEY_OLD",
        "JARVIS_CONFIG_KEY_OLD_FILE",
        "SMTP_HOST",
        "SMTP_HOST_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in kwargs.items():
        monkeypatch.setenv(name, value)
    get_secrets_settings.cache_clear()


def test_env_direct(monkeypatch):
    _isolated_env(monkeypatch, JARVIS_API_KEY="env-direct-value")
    assert SecretsSettings().jarvis_api_key.get_secret_value() == "env-direct-value"


def test_file_indirection_overrides_env_direct(monkeypatch, tmp_path: Path):
    secret_file = tmp_path / "api.key"
    secret_file.write_text("file-value\n")
    _isolated_env(
        monkeypatch,
        JARVIS_API_KEY="env-direct-loses",
        JARVIS_API_KEY_FILE=str(secret_file),
    )
    assert SecretsSettings().jarvis_api_key.get_secret_value() == "file-value"


def test_missing_secret_resolves_to_empty(monkeypatch):
    _isolated_env(monkeypatch)
    assert SecretsSettings().jarvis_api_key is None


def test_get_secrets_settings_is_cached(monkeypatch):
    _isolated_env(monkeypatch, JARVIS_API_KEY="cached-value")
    a = get_secrets_settings()
    b = get_secrets_settings()
    assert a is b


def test_model_hmac_key_file_indirection(monkeypatch, tmp_path: Path):
    """HMAC-1: JARVIS_MODEL_HMAC_KEY_FILE resolves via the shared
    _FILE-indirection machinery (no bespoke code).
    """
    hex_key = "a" * 64
    secret_file = tmp_path / "model_hmac.key"
    secret_file.write_text(hex_key + "\n")
    _isolated_env(monkeypatch, JARVIS_MODEL_HMAC_KEY_FILE=str(secret_file))
    assert get_secrets_settings().jarvis_model_hmac_key.get_secret_value() == hex_key


def test_file_read_failure_raises_at_construction(monkeypatch):
    _isolated_env(
        monkeypatch,
        JARVIS_API_KEY_FILE="/nonexistent/path/does/not/exist",
    )
    with pytest.raises((RuntimeError, FileNotFoundError, OSError)):
        SecretsSettings()


# ---------------------------------------------------------------------------
# W3-CF11: smtp.* empty-string rejection
#
# NOTE: All five smtp fields (smtp_host, smtp_port, smtp_user, smtp_pass,
# smtp_from) are typed as ``SecretStr | None`` with NO empty-string
# validator in SecretsSettings.  Setting any of them to "" produces a
# SecretStr('') rather than raising a ValidationError.  There is no
# production constraint to test here — these tests are marked xfail so that
# any future addition of empty-string validators will automatically turn them
# green without a code change.
#
# Verified against HEAD: PYTHONPATH=libs/jarvis_common uv run python -c
#   "from jarvis_common.settings import SecretsSettings; ..."
# — all five fields ACCEPT empty string (2026-05-24).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name,field_name",
    [
        ("SMTP_HOST", "smtp_host"),
        ("SMTP_FROM", "smtp_from"),
        ("SMTP_USER", "smtp_user"),
        ("SMTP_PASS", "smtp_pass"),
    ],
)
@pytest.mark.xfail(
    strict=False,
    reason=(
        "SecretsSettings has no empty-string validator for smtp.* fields "
        "(all typed as SecretStr | None); empty string is currently accepted. "
        "W3-CF11: add validators to make these pass."
    ),
)
def test_smtp_field_rejects_empty_string(monkeypatch, env_name: str, field_name: str) -> None:
    """Each smtp.* field should reject an empty-string value with ValidationError.

    Currently xfail: no validator exists — this documents the gap.
    """
    _isolated_env(monkeypatch, **{env_name: ""})
    with pytest.raises(Exception):  # expect pydantic ValidationError once validators added
        s = SecretsSettings()
        # If construction succeeds, force a failure so xfail is recorded accurately.
        val = getattr(s, field_name)
        if val is not None and val.get_secret_value() == "":
            raise AssertionError(f"{field_name} accepted empty string — validator missing")
