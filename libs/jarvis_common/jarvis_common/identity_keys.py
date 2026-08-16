"""Ed25519 key-file loading and rotation helpers for identity assertions."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from jarvis_common.config import JarvisCommonSettings
from jarvis_common.identity_assertions import (
    IdentityAssertionSigner,
    IdentityAssertionVerifier,
    VerificationKey,
)
from jarvis_common.identity_capabilities import IdentityAudience

_MAX_KEY_FILE_BYTES = 16 * 1024


class IdentityKeyConfigurationError(RuntimeError):
    """Raised when identity assertion key custody is misconfigured."""


def identity_key_id(public_key: Ed25519PublicKey) -> str:
    """Derive a stable non-secret key identifier from an Ed25519 public key.

    Parameters
    ----------
    public_key : Ed25519PublicKey
        Public key whose identifier is required.

    Returns
    -------
    str
        Base64url-encoded 96-bit SHA-256 prefix.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw).digest()[:12]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def load_identity_signer(
    private_key_file: str | Path,
    *,
    issuer: str,
) -> IdentityAssertionSigner:
    """Load the Platform-only signer from an unencrypted PEM secret file.

    Parameters
    ----------
    private_key_file : str or Path
        Docker-secret path containing one Ed25519 PKCS8 PEM private key.
    issuer : str
        Stable Platform issuer identifier.

    Returns
    -------
    IdentityAssertionSigner
        Signer whose ``kid`` is derived from the corresponding public key.

    Raises
    ------
    IdentityKeyConfigurationError
        If the file is missing, unreadable, oversized, malformed, encrypted,
        or contains a different private-key algorithm.
    """
    encoded = _read_key_file(private_key_file, label="private")
    try:
        key = serialization.load_pem_private_key(encoded, password=None)
    except (TypeError, ValueError) as exc:
        raise IdentityKeyConfigurationError(
            "identity private key must be an unencrypted PEM key"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise IdentityKeyConfigurationError("identity private key must use Ed25519")
    return IdentityAssertionSigner(
        issuer=issuer,
        key_id=identity_key_id(key.public_key()),
        signing_key=key,
    )


def load_identity_verifier(
    public_key_files: Sequence[str | Path],
    *,
    issuer: str,
    audience: str,
    previous_key_accept_until: datetime | None = None,
) -> IdentityAssertionVerifier:
    """Load current and optional overlap public keys for one backend.

    Parameters
    ----------
    public_key_files : Sequence[str or Path]
        One current Ed25519 PEM public key followed by at most one previous key.
    issuer : str
        Required Platform issuer identifier.
    audience : str
        Exact backend audience.
    previous_key_accept_until : datetime or None, optional
        Rotation-overlap deadline for the previous key. Required when a second
        key file is supplied and forbidden when only the current key is supplied.

    Returns
    -------
    IdentityAssertionVerifier
        Verifier configured with current and overlap keys.

    Raises
    ------
    IdentityKeyConfigurationError
        If the file count, overlap deadline, file contents, algorithm, or key
        identifiers are invalid.
    """
    paths = tuple(public_key_files)
    if not 1 <= len(paths) <= 2:
        raise IdentityKeyConfigurationError("one current and at most one previous key are required")
    if len(paths) == 2 and previous_key_accept_until is None:
        raise IdentityKeyConfigurationError("the previous key requires an overlap deadline")
    if len(paths) == 1 and previous_key_accept_until is not None:
        raise IdentityKeyConfigurationError("an overlap deadline requires a previous key")
    if previous_key_accept_until is not None and previous_key_accept_until.utcoffset() is None:
        raise IdentityKeyConfigurationError(
            "the previous-key overlap deadline must be timezone-aware"
        )

    records: dict[str, VerificationKey] = {}
    for index, path in enumerate(paths):
        encoded = _read_key_file(path, label="public")
        try:
            key = serialization.load_pem_public_key(encoded)
        except ValueError as exc:
            raise IdentityKeyConfigurationError("identity public key must be PEM encoded") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise IdentityKeyConfigurationError("identity public key must use Ed25519")
        key_id = identity_key_id(key)
        if key_id in records:
            raise IdentityKeyConfigurationError("identity public key files must be distinct")
        records[key_id] = VerificationKey(
            key,
            accept_until=previous_key_accept_until if index == 1 else None,
        )

    return IdentityAssertionVerifier(
        issuer=issuer,
        audience=audience,
        keys=records,
    )


def load_identity_verifier_from_settings(
    settings: JarvisCommonSettings,
    *,
    audience: IdentityAudience,
) -> IdentityAssertionVerifier:
    """Load one backend verifier from shared runtime settings.

    Parameters
    ----------
    settings : JarvisCommonSettings
        Runtime settings containing the issuer, key paths, and optional
        rotation-overlap deadline.
    audience : {"learning", "research"}
        Exact destination service whose assertions will be verified.

    Returns
    -------
    IdentityAssertionVerifier
        A verifier constrained to the configured issuer and exact audience.

    Raises
    ------
    IdentityKeyConfigurationError
        If key custody or rotation settings are invalid.
    """
    return load_identity_verifier(
        settings.identity_public_key_files,
        issuer=settings.identity_issuer,
        audience=audience,
        previous_key_accept_until=settings.identity_previous_key_accept_until,
    )


def _read_key_file(path_value: str | Path, *, label: str) -> bytes:
    path = Path(path_value)
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_KEY_FILE_BYTES:
            raise IdentityKeyConfigurationError(
                f"identity {label} key file must contain 1-{_MAX_KEY_FILE_BYTES} bytes"
            )
        encoded = path.read_bytes()
    except OSError as exc:
        raise IdentityKeyConfigurationError(f"identity {label} key file is unreadable") from exc
    if len(encoded) != size:
        raise IdentityKeyConfigurationError(f"identity {label} key file changed while loading")
    return encoded


__all__ = [
    "IdentityKeyConfigurationError",
    "identity_key_id",
    "load_identity_signer",
    "load_identity_verifier",
    "load_identity_verifier_from_settings",
]
