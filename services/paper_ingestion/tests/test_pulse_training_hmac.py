"""Unit tests for pulse/training.py _hmac_key() key-resolution.

Verifies that replacing ``os.environ.get("JARVIS_API_KEY")`` with the canonical
``get_secrets_settings().jarvis_api_key`` path produces byte-identical HMAC
digests compared to the old env-var path, and that both honour the
``JARVIS_API_KEY_FILE`` indirection provided by SecretsSettings.

Verified identifiers:
  pulse.training._hmac_key          training.py:18  — returns signing key bytes
  jarvis_common.settings.get_secrets_settings  settings.py:281  — cached factory
"""

from __future__ import annotations

import hashlib

import pytest
from jarvis_common.settings import get_secrets_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isolated_secrets(monkeypatch, **kwargs):
    """Remove all JARVIS_* secret env vars, then set only the given ones."""
    for name in (
        "JARVIS_API_KEY",
        "JARVIS_API_KEY_FILE",
        "JARVIS_MODEL_HMAC_KEY",
        "JARVIS_MODEL_HMAC_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in kwargs.items():
        monkeypatch.setenv(name, value)
    get_secrets_settings.cache_clear()


def _expected_digest_from_raw_env(raw_value: str) -> bytes:
    """Compute the digest the OLD code path would have produced."""
    return hashlib.sha256(b"model-signing:" + raw_value.encode()).digest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hmac_key_byte_identical_to_old_env_path(monkeypatch):
    """New settings path produces byte-identical digest to the old os.environ path.

    Critical: previously-signed pulse model blobs must still verify correctly.
    Computes both:
      old: hashlib.sha256(b"model-signing:" + os.environ["JARVIS_API_KEY"].encode())
      new: _hmac_key() via get_secrets_settings().jarvis_api_key
    and asserts they are equal.
    """
    _isolated_secrets(monkeypatch, JARVIS_API_KEY="test-api-key-for-hmac-parity")

    from paper_ingestion.pulse.training import _hmac_key

    new_key = _hmac_key()
    old_key = _expected_digest_from_raw_env("test-api-key-for-hmac-parity")
    assert new_key == old_key, f"HMAC digest mismatch: new={new_key.hex()!r}, old={old_key.hex()!r}"


def test_hmac_key_unset_raises_runtime_error(monkeypatch):
    """When neither JARVIS_MODEL_HMAC_KEY nor JARVIS_API_KEY is set, RuntimeError is raised.

    Preserves the exact behaviour of the old os.environ.get("JARVIS_API_KEY") path:
    no key → RuntimeError (not None, not empty bytes).
    Also ensures ENVIRONMENT != production so the production gate doesn't interfere.
    """
    _isolated_secrets(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "development")

    from paper_ingestion.pulse.training import _hmac_key

    with pytest.raises(RuntimeError, match="JARVIS_API_KEY"):
        _hmac_key()


def test_hmac_key_file_indirection_byte_identical(monkeypatch, tmp_path):
    """JARVIS_API_KEY_FILE indirection yields byte-identical digest to the env-direct path.

    The old os.environ.get path did NOT honour _FILE indirection; the new
    get_secrets_settings() path does.  For the common case where JARVIS_API_KEY
    is set directly, this test confirms the same bytes come through.

    Also verifies that when a _FILE is used (SecretsSettings feature), the
    resolved value is used for the HMAC — confirming the FILE path is
    transparently equivalent to env-direct for HMAC purposes.
    """
    secret_value = "file-indirection-secret-key"
    secret_file = tmp_path / "api.key"
    secret_file.write_text(secret_value + "\n")  # SecretsSettings strips trailing newline

    _isolated_secrets(monkeypatch, JARVIS_API_KEY_FILE=str(secret_file))

    from paper_ingestion.pulse.training import _hmac_key

    new_key = _hmac_key()
    expected = _expected_digest_from_raw_env(secret_value)
    assert new_key == expected, (
        f"FILE-indirection digest mismatch: new={new_key.hex()!r}, expected={expected.hex()!r}"
    )


def test_hmac_key_prefers_model_hmac_key_over_api_key(monkeypatch):
    """JARVIS_MODEL_HMAC_KEY is used preferentially when both keys are set.

    This is the existing resolution order (audit H14); the test confirms
    the api_key branch is not reached when model_hmac_key is present.
    """
    _isolated_secrets(
        monkeypatch,
        JARVIS_MODEL_HMAC_KEY="dedicated-model-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        JARVIS_API_KEY="should-not-be-used",
    )

    from paper_ingestion.pulse.training import _hmac_key

    result = _hmac_key()
    # Must equal encode() of the model key, NOT the api_key-derived digest
    expected = b"dedicated-model-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert result == expected
    # And must NOT equal the api_key derivation
    api_key_derived = _expected_digest_from_raw_env("should-not-be-used")
    assert result != api_key_derived


def test_hmac_key_multi_user_nonprod_refuses_derivation_fallback(monkeypatch):
    """SEC-4: a multi-user (JARVIS_SETUP_MODE != single) box refuses the
    api_key-derivation fallback even outside production — the dedicated
    JARVIS_MODEL_HMAC_KEY must be set."""
    _isolated_secrets(monkeypatch, JARVIS_API_KEY="bearer-only-no-dedicated-key")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")

    from paper_ingestion.pulse.training import _hmac_key

    with pytest.raises(RuntimeError, match="JARVIS_MODEL_HMAC_KEY"):
        _hmac_key()


def test_hmac_key_multi_user_nonprod_uses_dedicated_key(monkeypatch):
    """SEC-4: with the dedicated key set, a multi-user box derives no fallback."""
    _isolated_secrets(
        monkeypatch,
        JARVIS_MODEL_HMAC_KEY="dedicated-model-key-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
        JARVIS_API_KEY="should-not-be-used",
    )
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")

    from paper_ingestion.pulse.training import _hmac_key

    assert _hmac_key() == b"dedicated-model-key-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"


def test_hmac_key_single_user_nonprod_allows_derivation_fallback(monkeypatch):
    """SEC-4 non-regression: single-user dev still derives from JARVIS_API_KEY."""
    _isolated_secrets(monkeypatch, JARVIS_API_KEY="single-user-dev-key")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JARVIS_SETUP_MODE", "single")

    from paper_ingestion.pulse.training import _hmac_key

    assert _hmac_key() == _expected_digest_from_raw_env("single-user-dev-key")
