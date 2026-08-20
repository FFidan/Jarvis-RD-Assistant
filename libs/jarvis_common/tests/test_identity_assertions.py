"""Behavioral tests for signed internal identity assertions and their boot gate."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jarvis_common.auth import validate_production_config
from jarvis_common.identity_assertions import (
    AssertionReplayCache,
    IdentityAssertionError,
    IdentityAssertionSigner,
    IdentityAssertionVerifier,
    VerificationKey,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

#: Smallest environment that satisfies every other production gate, so the
#: identity gate is the only reason this configuration can be refused.
_PRODUCTION_ENVIRONMENT = {
    "ENVIRONMENT": "production",
    "DEV_MODE": "false",
    "JARVIS_API_KEY": "x" * 32,
    "JARVIS_MODEL_HMAC_KEY": "y" * 32,
    "JARVIS_CONFIG_KEY": "z" * 44,
    "LITELLM_MASTER_KEY": "sk-" + "p" * 40,
    "POSTGRES_PASSWORD": "q" * 24,
    "APP_BASE_URL": "https://jarvis.example.com",
}


def _signer(
    private_key: Ed25519PrivateKey, *, key_id: str = "platform-2026-08"
) -> IdentityAssertionSigner:
    return IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id=key_id,
        signing_key=private_key,
    )


def _verifier(
    private_key: Ed25519PrivateKey,
    *,
    issuer: str = "jarvis-platform",
    audience: str = "research",
    key_id: str = "platform-2026-08",
    accept_until: datetime | None = None,
) -> IdentityAssertionVerifier:
    return IdentityAssertionVerifier(
        issuer=issuer,
        audience=audience,
        keys={
            key_id: VerificationKey(
                private_key.public_key(),
                accept_until=accept_until,
            )
        },
    )


def _token(
    signer: IdentityAssertionSigner,
    *,
    audience: str = "research",
    scopes: tuple[str, ...] = ("papers:read",),
    now: datetime = NOW,
    not_before: datetime | None = None,
    ttl: timedelta = timedelta(seconds=60),
) -> str:
    return signer.issue(
        audience=audience,
        subject="user:42",
        principal="browser",
        request_id="request-123",
        request_method="GET",
        request_path="/api/papers",
        scopes=scopes,
        user_id=42,
        user_role="admin",
        session_id="session-456",
        now=now,
        not_before=not_before,
        ttl=ttl,
        token_id="id-1",
    )


def test_round_trip_returns_typed_claims() -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(_signer(private_key), scopes=("papers:write", "papers:read"))

    claims = _verifier(private_key).verify(
        token,
        required_scopes=("papers:read",),
        request_id="request-123",
        request_method="GET",
        request_path="/api/papers",
        now=NOW,
    )

    assert claims.user_id == 42
    assert claims.user_role == "admin"
    assert claims.audience == "research"
    assert claims.scopes == ("papers:read", "papers:write")
    assert claims.expires_at - claims.issued_at == 60


@pytest.mark.parametrize(
    ("audience", "required_scopes", "request_id", "message"),
    [
        ("research", ("papers:read",), "request-123", "issuer"),
        ("learning", ("papers:read",), "request-123", "audience"),
        ("research", ("papers:delete",), "request-123", "scope"),
        ("research", ("papers:read",), "request-elsewhere", "request binding"),
    ],
)
def test_destination_contract_rejects_mismatch(
    audience: str,
    required_scopes: tuple[str, ...],
    request_id: str,
    message: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(_signer(private_key))

    with pytest.raises(IdentityAssertionError, match=message):
        _verifier(
            private_key,
            issuer="different-platform" if message == "issuer" else "jarvis-platform",
            audience=audience,
        ).verify(
            token,
            required_scopes=required_scopes,
            request_id=request_id,
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


def test_replayed_assertion_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(_signer(private_key))
    verifier = _verifier(private_key)
    verifier.verify(
        token,
        required_scopes=("papers:read",),
        request_id="request-123",
        request_method="GET",
        request_path="/api/papers",
        now=NOW,
    )

    with pytest.raises(IdentityAssertionError, match="replayed"):
        verifier.verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


@pytest.mark.parametrize(
    ("request_method", "request_path", "message"),
    [
        ("POST", "/api/papers", "method binding"),
        ("GET", "/api/admin/users", "path binding"),
    ],
)
def test_operation_binding_rejects_redirected_assertion(
    request_method: str,
    request_path: str,
    message: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(_signer(private_key))

    with pytest.raises(IdentityAssertionError, match=message):
        _verifier(private_key).verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method=request_method,
            request_path=request_path,
            now=NOW,
        )


def test_expired_assertion_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(_signer(private_key), ttl=timedelta(seconds=10))

    with pytest.raises(IdentityAssertionError, match="expired"):
        _verifier(private_key).verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW + timedelta(seconds=13),
        )


def test_not_yet_valid_assertion_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(
        _signer(private_key),
        not_before=NOW + timedelta(seconds=10),
    )

    with pytest.raises(IdentityAssertionError, match="not yet valid"):
        _verifier(private_key).verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


def test_unknown_key_is_rejected() -> None:
    signing_key = Ed25519PrivateKey.generate()
    verification_key = Ed25519PrivateKey.generate()
    token = _token(_signer(signing_key))

    with pytest.raises(IdentityAssertionError, match="unknown"):
        _verifier(verification_key, key_id="different-key").verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


def test_tampered_assertion_is_rejected_without_consuming_replay_id() -> None:
    private_key = Ed25519PrivateKey.generate()
    token = _token(_signer(private_key))
    header, payload, signature = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"
    tampered = f"{header}.{payload[:-1]}{replacement}.{signature}"
    verifier = _verifier(private_key)

    with pytest.raises(IdentityAssertionError, match="signature"):
        verifier.verify(
            tampered,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )

    claims = verifier.verify(
        token,
        required_scopes=("papers:read",),
        request_id="request-123",
        request_method="GET",
        request_path="/api/papers",
        now=NOW,
    )
    assert claims.token_id == "id-1"


def test_previous_key_is_accepted_only_during_overlap() -> None:
    old_key = Ed25519PrivateKey.generate()
    token = _token(_signer(old_key, key_id="old-key"))

    during_overlap = _verifier(
        old_key,
        key_id="old-key",
        accept_until=NOW + timedelta(seconds=62),
    )
    assert (
        during_overlap.verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        ).user_id
        == 42
    )

    after_overlap = _verifier(
        old_key,
        key_id="old-key",
        accept_until=NOW - timedelta(seconds=1),
    )
    with pytest.raises(IdentityAssertionError, match="overlap"):
        after_overlap.verify(
            token,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


def test_signer_refuses_lifetimes_above_sixty_seconds() -> None:
    private_key = Ed25519PrivateKey.generate()

    with pytest.raises(ValueError, match="at most 60 seconds"):
        _token(_signer(private_key), ttl=timedelta(seconds=61))


def test_signer_rejects_non_integer_user_id() -> None:
    private_key = Ed25519PrivateKey.generate()

    with pytest.raises(ValueError, match="positive integer"):
        _signer(private_key).issue(
            audience="research",
            subject="user:42",
            principal="browser",
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            scopes=("papers:read",),
            user_id="42",  # type: ignore[arg-type] - exercise runtime boundary validation
            now=NOW,
        )


def test_verifier_rejects_inverted_validity_window() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = _signer(private_key)
    token = _token(signer)
    header, encoded_payload, _ = token.split(".")

    padding = "=" * (-len(encoded_payload) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
    payload["nbf"] = payload["exp"] + 1
    encoded_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signed = f"{header}.{encoded_payload}".encode()
    signature = base64.urlsafe_b64encode(private_key.sign(signed)).rstrip(b"=").decode("ascii")
    forged = f"{header}.{encoded_payload}.{signature}"

    with pytest.raises(IdentityAssertionError, match="validity window"):
        _verifier(private_key).verify(
            forged,
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )


def test_replay_cache_evicts_expired_entries_without_losing_live_entries() -> None:
    cache = AssertionReplayCache(maximum_entries=2)
    cache.consume("expired", expires_at=10, now=1)
    cache.consume("live", expires_at=20, now=1)

    cache.consume("replacement", expires_at=30, now=11)

    with pytest.raises(IdentityAssertionError, match="replayed"):
        cache.consume("live", expires_at=20, now=11)


def test_replay_cache_at_capacity_drops_its_oldest_live_entry_and_keeps_serving() -> None:
    """A registry full of live identifiers must not turn every caller away."""
    cache = AssertionReplayCache(maximum_entries=2)
    cache.consume("oldest", expires_at=60, now=1)
    cache.consume("newer", expires_at=60, now=1)

    cache.consume("newest", expires_at=60, now=1)

    for retained in ("newer", "newest"):
        with pytest.raises(IdentityAssertionError, match="replayed"):
            cache.consume(retained, expires_at=60, now=1)
    cache.consume("oldest", expires_at=60, now=1)


def test_production_configuration_refuses_a_disabled_identity_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A production process must not serve with Platform-signed identity off."""
    for name, value in _PRODUCTION_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JARVIS_IDENTITY_ASSERTIONS_REQUIRED", "false")

    with pytest.raises(RuntimeError, match="JARVIS_IDENTITY_ASSERTIONS_REQUIRED"):
        validate_production_config()

    monkeypatch.setenv("JARVIS_IDENTITY_ASSERTIONS_REQUIRED", "true")
    validate_production_config()


def test_verifier_rejects_oversized_compact_input_before_parsing() -> None:
    private_key = Ed25519PrivateKey.generate()

    with pytest.raises(IdentityAssertionError, match="too large"):
        _verifier(private_key).verify(
            "a" * (16 * 1024 + 1),
            required_scopes=("papers:read",),
            request_id="request-123",
            request_method="GET",
            request_path="/api/papers",
            now=NOW,
        )
