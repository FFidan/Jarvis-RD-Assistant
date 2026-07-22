"""Tests for the config-key rotation script's local validation seams."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


def _load_rotate_config_key():
    path = Path(__file__).resolve().parents[3] / "scripts" / "rotate_config_key.py"
    spec = importlib.util.spec_from_file_location("rotate_config_key_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rotate_config_key = _load_rotate_config_key()


def test_required_env_returns_present_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured environment values should pass through unchanged."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")

    assert rotate_config_key._required_env("DATABASE_URL") == "postgresql://example"


def test_required_env_raises_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing required settings should fail before any database connection."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(
        (rotate_config_key.ScriptError, SystemExit), match="DATABASE_URL is required"
    ):
        rotate_config_key._required_env("DATABASE_URL")


def test_required_env_raises_script_error_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """_required_env must raise ScriptError (not SystemExit) outside __main__."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(rotate_config_key.ScriptError, match="DATABASE_URL is required"):
        rotate_config_key._required_env("DATABASE_URL")


def test_required_secret_reads_file_indirection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rotation keys can be mounted as files instead of exposed in container env."""
    secret_file = tmp_path / "rotation-key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.delenv("OLD_JARVIS_CONFIG_KEY", raising=False)
    monkeypatch.setenv("OLD_JARVIS_CONFIG_KEY_FILE", str(secret_file))

    assert rotate_config_key._required_secret("OLD_JARVIS_CONFIG_KEY") == "file-secret"


def test_required_secret_prefers_direct_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The existing direct environment contract remains backward compatible."""
    secret_file = tmp_path / "rotation-key"
    secret_file.write_text("file-secret", encoding="utf-8")
    monkeypatch.setenv("NEW_JARVIS_CONFIG_KEY", "env-secret")
    monkeypatch.setenv("NEW_JARVIS_CONFIG_KEY_FILE", str(secret_file))

    assert rotate_config_key._required_secret("NEW_JARVIS_CONFIG_KEY") == "env-secret"


def test_database_url_uses_mounted_postgres_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prebuilt application image can connect without host Python packages."""
    password_file = tmp_path / "postgres-password"
    password_file.write_text("p@ss/word%\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("POSTGRES_USER", "jarvis user")
    monkeypatch.setenv("POSTGRES_DB", "jarvis/db")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")

    assert rotate_config_key._database_url() == (
        "postgresql://jarvis%20user:p%40ss%2Fword%25@postgres:5432/jarvis%2Fdb"
    )


def test_database_url_requires_password_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing direct URL and missing password file fail before connecting."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD_FILE", raising=False)

    with pytest.raises(
        rotate_config_key.ScriptError,
        match="DATABASE_URL or POSTGRES_PASSWORD_FILE is required",
    ):
        rotate_config_key._database_url()


@pytest.mark.parametrize(
    ("ciphertext_source", "expected"),
    [
        ("empty", ("empty", 0)),
        ("old", ("old", 1)),
        ("new", ("new", 1)),
        ("mixed", ("ambiguous", 2)),
    ],
)
def test_classify_key_state_distinguishes_empty_old_new_and_mixed_rows(
    ciphertext_source: str,
    expected: tuple[str, int],
) -> None:
    old = Fernet(Fernet.generate_key())
    new = Fernet(Fernet.generate_key())
    ciphertexts: list[bytes]
    if ciphertext_source == "empty":
        ciphertexts = []
    elif ciphertext_source == "old":
        ciphertexts = [old.encrypt(b"value")]
    elif ciphertext_source == "new":
        ciphertexts = [new.encrypt(b"value")]
    else:
        ciphertexts = [old.encrypt(b"old"), new.encrypt(b"new")]

    assert rotate_config_key._classify_key_state(ciphertexts, old, new) == expected


def test_classify_key_state_treats_two_equivalent_keys_as_ambiguous() -> None:
    key = Fernet(Fernet.generate_key())

    assert rotate_config_key._classify_key_state([key.encrypt(b"value")], key, key) == (
        "ambiguous",
        1,
    )
