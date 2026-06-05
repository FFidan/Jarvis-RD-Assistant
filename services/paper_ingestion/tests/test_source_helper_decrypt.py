"""Tests for _decrypt_config_api_key in paper_ingestion.services.source_helper (PI-CFG-03).

Covers:
  - Encrypted api_key is decrypted correctly before passing to source constructor.
  - Legacy plaintext api_key falls back gracefully (no InvalidToken propagation).
  - Config without api_key is returned unchanged.
  - JARVIS_CONFIG_KEY unset (RuntimeError) falls back to plaintext.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from jarvis_common.crypto import encrypt_secret, refresh_fernet_cache


@pytest.fixture()
def _fernet_key(monkeypatch):
    """Wire a fresh Fernet key into JARVIS_CONFIG_KEY for the test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key)
    refresh_fernet_cache()
    yield
    refresh_fernet_cache()


@pytest.mark.usefixtures("_fernet_key")
def test_decrypt_config_api_key_decrypts_ciphertext():
    """Fernet ciphertext in config is decrypted to plaintext before source use."""
    from paper_ingestion.services.source_helper import _decrypt_config_api_key

    ciphertext = encrypt_secret("s2-api-key-plaintext")
    result = _decrypt_config_api_key({"api_key": ciphertext, "email": "a@b.com"})

    assert result["api_key"] == "s2-api-key-plaintext"
    assert result["email"] == "a@b.com"


@pytest.mark.usefixtures("_fernet_key")
def test_decrypt_config_api_key_legacy_plaintext_fallback():
    """A legacy plaintext api_key (not a valid Fernet token) is kept as-is."""
    from paper_ingestion.services.source_helper import _decrypt_config_api_key

    result = _decrypt_config_api_key({"api_key": "legacy-plain-key"})

    assert result["api_key"] == "legacy-plain-key"


def test_decrypt_config_api_key_no_key_env(monkeypatch):
    """When JARVIS_CONFIG_KEY is unset, fallback keeps the raw value."""
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    refresh_fernet_cache()
    try:
        from paper_ingestion.services.source_helper import _decrypt_config_api_key

        result = _decrypt_config_api_key({"api_key": "some-raw-value"})
        assert result["api_key"] == "some-raw-value"
    finally:
        refresh_fernet_cache()


def test_decrypt_config_api_key_absent():
    """Config without api_key is returned unchanged."""
    from paper_ingestion.services.source_helper import _decrypt_config_api_key

    cfg = {"email": "user@example.com"}
    result = _decrypt_config_api_key(cfg)
    assert result == cfg


def test_decrypt_config_api_key_empty_string():
    """Empty api_key string is treated as absent and config returned unchanged."""
    from paper_ingestion.services.source_helper import _decrypt_config_api_key

    cfg = {"api_key": ""}
    result = _decrypt_config_api_key(cfg)
    assert result is cfg


# ---------------------------------------------------------------------------
# _init_source_singletons: encrypted api_key is decrypted before source ctor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fernet_key")
async def test_init_source_singletons_decrypts_api_key():
    """_init_source_singletons must decrypt an encrypted api_key before constructing the source.

    PI-CFG-03 gap: the singleton path previously passed raw row["config"] (which
    may contain a Fernet ciphertext) directly to PaperSourceConfig, so cached
    singletons would receive the encrypted bytes as the api_key.  This test
    verifies the fix: the config passed to the source constructor has the
    api_key decrypted to plaintext.

    Approach: spy the source constructor and capture the PaperSourceConfig.config
    dict.  Because _init_source_singletons is tightly coupled to the FastAPI
    lifespan (DB pool, http_client, source registry), we call it directly with a
    mock pool/app, patching get_source_class to return a SpySource.  This is the
    narrowest seam available without a full lifespan harness.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi import FastAPI
    from jarvis_common.crypto import encrypt_secret, refresh_fernet_cache
    from paper_ingestion.main import _init_source_singletons
    from paper_ingestion.models import SourceType

    # Build a fresh Fernet key and seed it (fixture already wired one; this
    # ensures encrypt_secret and the decrypt helper share the same key cache).
    refresh_fernet_cache()

    plaintext_key = "s2-secret-key-123"
    ciphertext = encrypt_secret(plaintext_key)

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # Only arxiv is tested; return encrypted api_key in config.
    conn.fetchrow.return_value = {
        "id": 1,
        "source_type": "arxiv",
        "enabled": True,
        "config": {"api_key": ciphertext},
    }

    received_configs: list[dict] = []

    class SpySource:
        def __init__(self, config, http_client, db_pool=None):
            received_configs.append(dict(config.config))

    app = FastAPI()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()

    def _spy_get_source_class(source_type_val: str):
        if source_type_val == SourceType.ARXIV.value:
            return SpySource
        return None  # skip other sources

    with (
        patch("paper_ingestion.main.get_source_class", side_effect=_spy_get_source_class),
        patch("paper_ingestion._state.set_services"),
    ):
        await _init_source_singletons(app)

    assert received_configs, "_init_source_singletons did not construct any source"
    arxiv_cfg = received_configs[0]
    assert arxiv_cfg.get("api_key") == plaintext_key, (
        f"Expected decrypted api_key={plaintext_key!r} in singleton config, "
        f"got {arxiv_cfg.get('api_key')!r} — encrypted ciphertext was passed to source ctor"
    )
