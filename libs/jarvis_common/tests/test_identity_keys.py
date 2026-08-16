"""Boundary tests for Ed25519 identity key custody and rotation loading."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jarvis_common.identity_assertions import IdentityAssertionError
from jarvis_common.identity_keys import (
    IdentityKeyConfigurationError,
    identity_key_id,
    load_identity_signer,
    load_identity_verifier,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _write_key_pair(directory: Path, name: str) -> tuple[Path, Path, str]:
    private_key = Ed25519PrivateKey.generate()
    private_path = directory / f"{name}-private.pem"
    public_path = directory / f"{name}-public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path, identity_key_id(private_key.public_key())


def test_loaded_signer_and_verifier_round_trip(tmp_path: Path) -> None:
    private_path, public_path, _ = _write_key_pair(tmp_path, "current")
    signer = load_identity_signer(private_path, issuer="jarvis-platform")
    verifier = load_identity_verifier(
        [public_path],
        issuer="jarvis-platform",
        audience="research",
    )
    token = signer.issue(
        audience="research",
        subject="user:7",
        principal="browser",
        request_id="request-1",
        request_method="GET",
        request_path="/api/papers",
        scopes=("papers:read",),
        user_id=7,
        now=NOW,
    )

    claims = verifier.verify(
        token,
        required_scopes=("papers:read",),
        request_id="request-1",
        request_method="GET",
        request_path="/api/papers",
        now=NOW,
    )

    assert claims.user_id == 7


def test_current_and_previous_keys_follow_overlap_deadline(tmp_path: Path) -> None:
    current_private, current_public, _ = _write_key_pair(tmp_path, "current")
    previous_private, previous_public, _ = _write_key_pair(tmp_path, "previous")
    verifier = load_identity_verifier(
        [current_public, previous_public],
        issuer="jarvis-platform",
        audience="research",
        previous_key_accept_until=NOW + timedelta(seconds=62),
    )
    current_token = load_identity_signer(current_private, issuer="jarvis-platform").issue(
        audience="research",
        subject="user:7",
        principal="browser",
        request_id="request-current",
        request_method="GET",
        request_path="/api/papers",
        scopes=("papers:read",),
        user_id=7,
        now=NOW,
    )
    previous_token = load_identity_signer(previous_private, issuer="jarvis-platform").issue(
        audience="research",
        subject="user:7",
        principal="browser",
        request_id="request-previous",
        request_method="GET",
        request_path="/api/papers",
        scopes=("papers:read",),
        user_id=7,
        now=NOW,
    )

    assert (
        verifier.verify(
            current_token,
            required_scopes=("papers:read",),
            request_id="request-current",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        ).user_id
        == 7
    )
    assert (
        verifier.verify(
            previous_token,
            required_scopes=("papers:read",),
            request_id="request-previous",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        ).user_id
        == 7
    )

    verifier_after_overlap = load_identity_verifier(
        [current_public, previous_public],
        issuer="jarvis-platform",
        audience="research",
        previous_key_accept_until=NOW - timedelta(seconds=1),
    )
    with pytest.raises(IdentityAssertionError, match="overlap"):
        verifier_after_overlap.verify(
            previous_token,
            required_scopes=("papers:read",),
            request_id="request-previous",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


def test_previous_key_requires_explicit_overlap_deadline(tmp_path: Path) -> None:
    _, current_public, _ = _write_key_pair(tmp_path, "current")
    _, previous_public, _ = _write_key_pair(tmp_path, "previous")

    with pytest.raises(IdentityKeyConfigurationError, match="overlap deadline"):
        load_identity_verifier(
            [current_public, previous_public],
            issuer="jarvis-platform",
            audience="research",
        )


def test_previous_key_requires_timezone_aware_overlap_deadline(tmp_path: Path) -> None:
    _, current_public, _ = _write_key_pair(tmp_path, "current")
    _, previous_public, _ = _write_key_pair(tmp_path, "previous")

    with pytest.raises(IdentityKeyConfigurationError, match="timezone-aware"):
        load_identity_verifier(
            [current_public, previous_public],
            issuer="jarvis-platform",
            audience="research",
            previous_key_accept_until=datetime(2026, 8, 16, 12, 0),
        )


def test_private_key_cannot_be_loaded_as_public_configuration(tmp_path: Path) -> None:
    private_path, _, _ = _write_key_pair(tmp_path, "current")

    with pytest.raises(IdentityKeyConfigurationError, match="public key"):
        load_identity_verifier(
            [private_path],
            issuer="jarvis-platform",
            audience="research",
        )


def test_wrong_key_algorithm_is_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "not-a-key.pem"
    invalid.write_text("not a PEM key", encoding="utf-8")

    with pytest.raises(IdentityKeyConfigurationError, match="PEM"):
        load_identity_verifier(
            [invalid],
            issuer="jarvis-platform",
            audience="research",
        )
