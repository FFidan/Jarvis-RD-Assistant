"""Tests for jarvis_common.crypto envelope-encryption module."""

from __future__ import annotations

import signal
import threading

import pytest
from cryptography.fernet import Fernet, InvalidToken
from jarvis_common.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    refresh_fernet_cache,
    reload_fernet_on_sighup,
    resolve_secret_row,
    validate_encrypted_config_rows,
)
from jarvis_common.testing import FakeRecord, make_pool_and_conn


@pytest.fixture()
def valid_key(monkeypatch) -> bytes:
    """Generate a fresh Fernet key and wire it into JARVIS_CONFIG_KEY."""
    key = Fernet.generate_key()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key.decode())
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    return key


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("valid_key")
def test_roundtrip() -> None:
    """encrypt_secret then decrypt_secret returns the original plaintext."""
    plaintext = "super-secret-api-key-12345"
    ciphertext = encrypt_secret(plaintext)
    assert decrypt_secret(ciphertext) == plaintext


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("valid_key")
def test_tamper_raises() -> None:
    """Modifying a character of the ciphertext causes decrypt to raise InvalidToken."""
    ciphertext = encrypt_secret("my-secret")
    # Flip the last character
    tampered = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
    with pytest.raises(InvalidToken):
        decrypt_secret(tampered)


# ---------------------------------------------------------------------------
# Wrong key
# ---------------------------------------------------------------------------


def test_wrong_key_raises(monkeypatch) -> None:
    """Ciphertext encrypted under one key cannot be decrypted with another."""
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()

    # Encrypt with key_a
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key_a.decode())
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    ciphertext = encrypt_secret("secret-value")

    # Switch to key_b and attempt decryption
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key_b.decode())
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext)


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


def test_rotation_via_old_env_decrypts_legacy_ciphertext(monkeypatch) -> None:
    """JARVIS_CONFIG_KEY_OLD lets the rotated process decrypt legacy ciphertexts.

    Encrypt a value under ``key_a``, then start a process with ``key_b`` as
    JARVIS_CONFIG_KEY and ``key_a`` as JARVIS_CONFIG_KEY_OLD. ``decrypt_secret``
    must succeed (zero-downtime rotation), and a fresh ``encrypt_secret`` must
    use the new key.
    """
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()

    # Encrypt under key_a (the soon-to-be-old key)
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key_a.decode())
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    legacy_ciphertext = encrypt_secret("rotation-target")

    # Switch to key_b as the new key, with key_a kept as OLD for reads.
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key_b.decode())
    monkeypatch.setenv("JARVIS_CONFIG_KEY_OLD", key_a.decode())
    refresh_fernet_cache()

    # Old ciphertext still decrypts.
    assert decrypt_secret(legacy_ciphertext) == "rotation-target"

    # New writes use key_b — verify by trying to decrypt with key_b directly.
    new_ciphertext = encrypt_secret("post-rotation-write")
    assert Fernet(key_b).decrypt(new_ciphertext.encode()).decode() == "post-rotation-write"


# ---------------------------------------------------------------------------
# Missing env var
# ---------------------------------------------------------------------------


def test_missing_env_raises(monkeypatch) -> None:
    """Calling encrypt_secret without JARVIS_CONFIG_KEY raises RuntimeError."""
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    with pytest.raises(RuntimeError, match="JARVIS_CONFIG_KEY"):
        encrypt_secret("anything")


# ---------------------------------------------------------------------------
# mask_secret
# ---------------------------------------------------------------------------


def test_mask_secret() -> None:
    """mask_secret covers empty, short (< 4), and long inputs.

    The masked form must NEVER include the first 4 characters of the secret —
    leaking the prefix would reveal e.g. ``sk-ant-`` for Anthropic keys.
    """
    assert mask_secret("") == ""
    assert mask_secret("ab") == "****"
    assert mask_secret("abc") == "****"
    assert mask_secret("abcd") == "****abcd"
    assert mask_secret("abcde") == "****bcde"
    assert mask_secret("supersecret") == "****cret"


def test_mask_secret_does_not_leak_prefix() -> None:
    """Masking a provider-prefixed key must not reveal the prefix.

    Regression guard against the previous ``plaintext[:4] + "****"`` form
    which leaked the first four characters (e.g. ``sk-a****`` exposed that
    the secret is an Anthropic key).
    """
    secret = "sk-ant-api03-supersecret-verylong"
    masked = mask_secret(secret)
    assert "sk-a" not in masked
    assert not masked.startswith(secret[:4])
    # Trailing 4 chars are fine — they cannot be reverse-engineered to the prefix.
    assert masked.endswith(secret[-4:])


@pytest.mark.asyncio
async def test_validate_encrypted_config_rows_accepts_decryptable_rows(valid_key) -> None:
    ciphertext = Fernet(valid_key).encrypt(b"secret")
    pool, conn = make_pool_and_conn(
        fetch_return=[FakeRecord({"key": "llm.openai.api_key", "encrypted_value": ciphertext})]
    )

    checked = await validate_encrypted_config_rows(pool, dev_mode=False)

    assert checked == 1
    conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_encrypted_config_rows_fails_non_dev_on_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    pool, _conn = make_pool_and_conn(
        fetch_return=[FakeRecord({"key": "zotero.api_key", "encrypted_value": b"abc"})]
    )

    with pytest.raises(RuntimeError, match="Encrypted user_config rows exist"):
        await validate_encrypted_config_rows(pool, dev_mode=False)


@pytest.mark.asyncio
async def test_validate_encrypted_config_rows_warns_in_dev_on_bad_key(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    pool, _conn = make_pool_and_conn(
        fetch_return=[FakeRecord({"key": "zotero.api_key", "encrypted_value": b"abc"})]
    )

    checked = await validate_encrypted_config_rows(pool, dev_mode=True)

    assert checked == 1


# ---------------------------------------------------------------------------
# resolve_secret_row helper (Sprint 7 B12)
# ---------------------------------------------------------------------------


def test_resolve_secret_row_decrypts_encrypted_value(valid_key) -> None:
    ciphertext = encrypt_secret("plaintext-secret")
    row = FakeRecord(
        {"key": "zotero.api_key", "value": None, "encrypted_value": ciphertext.encode()}
    )
    assert resolve_secret_row(row) == "plaintext-secret"


def test_resolve_secret_row_returns_legacy_plaintext(valid_key) -> None:
    row = FakeRecord(
        {"key": "zotero.api_key", "value": "legacy-plaintext", "encrypted_value": None}
    )
    assert resolve_secret_row(row) == "legacy-plaintext"


def test_resolve_secret_row_returns_none_when_both_missing(valid_key) -> None:
    row = FakeRecord({"key": "zotero.api_key", "value": None, "encrypted_value": None})
    assert resolve_secret_row(row) is None


def test_resolve_secret_row_raises_invalid_token_on_bad_ciphertext(valid_key) -> None:
    row = FakeRecord(
        {"key": "zotero.api_key", "value": None, "encrypted_value": b"not-real-ciphertext"}
    )
    with pytest.raises(InvalidToken):
        resolve_secret_row(row)


def test_resolve_secret_row_handles_memoryview_ciphertext(valid_key) -> None:
    ciphertext = encrypt_secret("from-memoryview").encode()
    row = FakeRecord(
        {"key": "zotero.api_key", "value": None, "encrypted_value": memoryview(ciphertext)}
    )
    assert resolve_secret_row(row) == "from-memoryview"


# ---------------------------------------------------------------------------
# resolve_secret_row — tightened typing (W4-T4)
# ---------------------------------------------------------------------------


def test_resolve_secret_row_dict_encrypted(valid_key) -> None:
    """Plain dict with encrypted_value → returns decrypted value."""
    ciphertext = encrypt_secret("dict-encrypted-secret")
    row: dict[str, object] = {"encrypted_value": ciphertext.encode(), "value": None}
    assert resolve_secret_row(row) == "dict-encrypted-secret"


def test_resolve_secret_row_dict_legacy_plaintext(valid_key) -> None:
    """Plain dict with value (legacy plaintext path) → returns it directly."""
    row: dict[str, object] = {"encrypted_value": None, "value": "legacy-via-dict"}
    assert resolve_secret_row(row) == "legacy-via-dict"


def test_resolve_secret_row_none_input() -> None:
    """Passing None returns None without raising."""
    assert resolve_secret_row(None) is None


# ---------------------------------------------------------------------------
# reload_fernet_on_sighup — idempotency + thread-safety guard
# ---------------------------------------------------------------------------


def test_reload_fernet_on_sighup_idempotent() -> None:
    """Calling reload_fernet_on_sighup multiple times does not raise."""
    reload_fernet_on_sighup()
    reload_fernet_on_sighup()
    reload_fernet_on_sighup()


def test_reload_fernet_on_sighup_registers_handler() -> None:
    """After registration, SIGHUP handler is not SIG_DFL."""
    reload_fernet_on_sighup()
    handler = signal.getsignal(signal.SIGHUP)
    assert handler not in (signal.SIG_DFL, signal.SIG_IGN)


def test_reload_fernet_on_sighup_safe_from_non_main_thread() -> None:
    """reload_fernet_on_sighup must not raise when called from a non-main thread."""
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            reload_fernet_on_sighup()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    assert errors == []


def test_resolve_secret_row_custom_mapping(valid_key) -> None:
    """A custom Mapping subclass (no .get override) still works correctly."""
    from collections.abc import Iterator
    from collections.abc import Mapping as AbcMapping

    class MinimalMapping(AbcMapping[str, object]):
        """Implements only the three abstract methods required by Mapping."""

        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def __getitem__(self, key: str) -> object:
            return self._data[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    ciphertext = encrypt_secret("mapping-secret")
    row = MinimalMapping({"encrypted_value": ciphertext.encode(), "value": None})
    assert resolve_secret_row(row) == "mapping-secret"
