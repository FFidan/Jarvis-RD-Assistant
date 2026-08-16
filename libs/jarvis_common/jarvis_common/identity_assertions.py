"""Signed identity assertions for trusted service-to-service requests.

The dashboard gateway asks the Platform service to mint a short-lived compact
JWS for one backend request. Research and Learning verify that assertion before
placing identity on ``request.state``. Browser-supplied identity headers are
never trusted directly.
"""

from __future__ import annotations

import base64
import binascii
import heapq
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_ALGORITHM: Final = "EdDSA"
_TOKEN_TYPE: Final = "JWT"
_FORMAT_VERSION: Final = 1
_MAX_TTL: Final = timedelta(seconds=60)
_MAX_COMPACT_LENGTH: Final = 16 * 1024
_HEADER_FIELDS: Final = frozenset({"alg", "kid", "typ"})
_CLAIM_FIELDS: Final = frozenset(
    {
        "aud",
        "exp",
        "iat",
        "iss",
        "jti",
        "mth",
        "nbf",
        "prn",
        "pth",
        "rid",
        "role",
        "scp",
        "sid",
        "sub",
        "uid",
        "ver",
    }
)


class IdentityAssertionError(ValueError):
    """Raised when an internal identity assertion is invalid or unauthorized."""


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    """Verified identity and capability claims carried by an assertion.

    Parameters
    ----------
    issuer : str
        Platform issuer identifier.
    audience : str
        Exact destination service identifier.
    subject : str
        Stable user or service subject.
    principal : str
        Calling principal, such as ``browser`` or ``telegram``.
    user_id : int or None
        Authenticated JARVIS user ID, when the principal acts for a user.
    user_role : str or None
        Authenticated user role.
    session_id : str or None
        Platform session identifier, when session authentication was used.
    request_id : str
        Request identifier bound to the assertion.
    request_method : str
        Uppercase HTTP method bound to the assertion.
    request_path : str
        Absolute request path bound to the assertion.
    scopes : tuple[str, ...]
        Sorted, unique capabilities granted to the destination request.
    issued_at : int
        Unix timestamp at which Platform issued the assertion.
    not_before : int
        Earliest valid Unix timestamp.
    expires_at : int
        Expiration Unix timestamp.
    token_id : str
        Unique replay identifier.
    """

    issuer: str
    audience: str
    subject: str
    principal: str
    user_id: int | None
    user_role: str | None
    session_id: str | None
    request_id: str
    request_method: str
    request_path: str
    scopes: tuple[str, ...]
    issued_at: int
    not_before: int
    expires_at: int
    token_id: str

    def to_payload(self) -> dict[str, object]:
        """Return the canonical versioned JWS payload.

        Returns
        -------
        dict[str, object]
            JSON-compatible claims using registered short claim names.
        """
        return {
            "aud": self.audience,
            "exp": self.expires_at,
            "iat": self.issued_at,
            "iss": self.issuer,
            "jti": self.token_id,
            "mth": self.request_method,
            "nbf": self.not_before,
            "prn": self.principal,
            "pth": self.request_path,
            "rid": self.request_id,
            "role": self.user_role,
            "scp": list(self.scopes),
            "sid": self.session_id,
            "sub": self.subject,
            "uid": self.user_id,
            "ver": _FORMAT_VERSION,
        }


@dataclass(frozen=True, slots=True)
class VerificationKey:
    """Ed25519 public key and optional rotation-overlap deadline.

    Parameters
    ----------
    public_key : Ed25519PublicKey
        Public verification key.
    accept_until : datetime or None
        Last instant at which an assertion using this key is accepted. ``None``
        denotes the current key.
    """

    public_key: Ed25519PublicKey
    accept_until: datetime | None = None


class AssertionReplayCache:
    """Bounded in-memory first-use registry for short-lived assertions."""

    def __init__(self, *, maximum_entries: int = 10_000) -> None:
        """Initialize an empty replay registry.

        Parameters
        ----------
        maximum_entries : int, default=10000
            Hard bound on unexpired assertion identifiers.

        Raises
        ------
        ValueError
            If ``maximum_entries`` is not positive.
        """
        if maximum_entries <= 0:
            raise ValueError("maximum_entries must be positive")
        self._maximum_entries = maximum_entries
        self._expires_at: dict[str, int] = {}
        self._expiry_heap: list[tuple[int, str]] = []

    def consume(self, token_id: str, *, expires_at: int, now: int) -> None:
        """Record first use of an assertion identifier.

        Parameters
        ----------
        token_id : str
            Unique assertion identifier.
        expires_at : int
            Assertion expiration timestamp.
        now : int
            Current Unix timestamp used to remove expired entries.

        Raises
        ------
        IdentityAssertionError
            If ``token_id`` was already consumed or the cache is full after
            expired entries are removed.
        """
        while self._expiry_heap and self._expiry_heap[0][0] < now:
            expiry, expired_token_id = heapq.heappop(self._expiry_heap)
            if self._expires_at.get(expired_token_id) == expiry:
                del self._expires_at[expired_token_id]
        if token_id in self._expires_at:
            raise IdentityAssertionError("identity assertion was replayed")
        if len(self._expires_at) >= self._maximum_entries:
            raise IdentityAssertionError("identity assertion replay cache is full")
        self._expires_at[token_id] = expires_at
        heapq.heappush(self._expiry_heap, (expires_at, token_id))


class IdentityAssertionSigner:
    """Mint short-lived Ed25519 compact JWS identity assertions."""

    def __init__(
        self,
        *,
        issuer: str,
        key_id: str,
        signing_key: Ed25519PrivateKey,
    ) -> None:
        """Initialize the Platform-only assertion signer.

        Parameters
        ----------
        issuer : str
            Stable issuer identifier expected by every backend.
        key_id : str
            Published identifier for ``signing_key``.
        signing_key : Ed25519 key
            Platform signing key. Domain services must never receive it.

        Raises
        ------
        ValueError
            If ``issuer`` or ``key_id`` is empty.
        """
        self._issuer = _required_text(issuer, "issuer")
        self._key_id = _required_text(key_id, "key_id")
        self._private_key = signing_key

    def issue(  # noqa: PLR0913 - cryptographic claim schema is explicit at the boundary
        self,
        *,
        audience: str,
        subject: str,
        principal: str,
        request_id: str,
        request_method: str,
        request_path: str,
        scopes: tuple[str, ...],
        user_id: int | None = None,
        user_role: str | None = None,
        session_id: str | None = None,
        ttl: timedelta = _MAX_TTL,
        now: datetime | None = None,
        not_before: datetime | None = None,
        token_id: str | None = None,
    ) -> str:
        """Issue one destination-scoped identity assertion.

        Parameters
        ----------
        audience : str
            Exact destination service.
        subject : str
            Stable user or service subject.
        principal : str
            Calling principal.
        request_id : str
            Request identifier bound to the assertion.
        request_method : str
            HTTP method bound to the assertion.
        request_path : str
            Absolute path bound to the assertion. Query parameters are excluded.
        scopes : tuple[str, ...]
            Minimum destination capabilities.
        user_id : int or None, optional
            Authenticated JARVIS user ID.
        user_role : str or None, optional
            Authenticated user role.
        session_id : str or None, optional
            Platform session identifier.
        ttl : timedelta, default=60 seconds
            Assertion lifetime. Values above 60 seconds are forbidden.
        now : datetime or None, optional
            Issuance time. Defaults to the current UTC time.
        not_before : datetime or None, optional
            Earliest valid time. Defaults to ``now``.
        token_id : str or None, optional
            Replay identifier. Defaults to a random UUID.

        Returns
        -------
        str
            Compact Ed25519 JWS.

        Raises
        ------
        ValueError
            If required claims are empty, types are invalid, scopes are empty,
            or ``ttl`` is outside ``(0, 60 seconds]``.
        """
        issued = _utc_datetime(now or datetime.now(UTC), "now")
        valid_from = _utc_datetime(not_before or issued, "not_before")
        if ttl <= timedelta(0) or ttl > _MAX_TTL:
            raise ValueError("ttl must be greater than zero and at most 60 seconds")
        if valid_from > issued + ttl:
            raise ValueError("not_before must not be later than expiration")
        normalized_scopes = _normalize_scopes(scopes)
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
        ):
            raise ValueError("user_id must be a positive integer or None")

        claims = IdentityClaims(
            issuer=self._issuer,
            audience=_required_text(audience, "audience"),
            subject=_required_text(subject, "subject"),
            principal=_required_text(principal, "principal"),
            user_id=user_id,
            user_role=_optional_text(user_role, "user_role"),
            session_id=_optional_text(session_id, "session_id"),
            request_id=_required_text(request_id, "request_id"),
            request_method=_request_method(request_method),
            request_path=_request_path(request_path),
            scopes=normalized_scopes,
            issued_at=int(issued.timestamp()),
            not_before=int(valid_from.timestamp()),
            expires_at=int((issued + ttl).timestamp()),
            token_id=_required_text(token_id or str(uuid.uuid4()), "token_id"),
        )
        header = {"alg": _ALGORITHM, "kid": self._key_id, "typ": _TOKEN_TYPE}
        encoded_header = _encode_json(header)
        encoded_payload = _encode_json(claims.to_payload())
        signed = f"{encoded_header}.{encoded_payload}".encode()
        signature = _encode_bytes(self._private_key.sign(signed))
        return f"{encoded_header}.{encoded_payload}.{signature}"


class IdentityAssertionVerifier:
    """Verify destination-scoped assertions and reject replay."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        keys: Mapping[str, VerificationKey],
        replay_cache: AssertionReplayCache | None = None,
        clock_skew: timedelta = timedelta(seconds=2),
    ) -> None:
        """Initialize a backend assertion verifier.

        Parameters
        ----------
        issuer : str
            Required Platform issuer.
        audience : str
            Exact destination service.
        keys : Mapping[str, VerificationKey]
            Current and overlap public keys keyed by ``kid``.
        replay_cache : AssertionReplayCache or None, optional
            First-use registry. A private registry is created by default.
        clock_skew : timedelta, default=2 seconds
            Maximum accepted time skew.

        Raises
        ------
        ValueError
            If configuration is empty or ``clock_skew`` is negative or greater
            than the 60-second assertion lifetime.
        """
        self._issuer = _required_text(issuer, "issuer")
        self._audience = _required_text(audience, "audience")
        if not keys:
            raise ValueError("at least one verification key is required")
        self._keys = dict(keys)
        for key_id in self._keys:
            _required_text(key_id, "key_id")
        if clock_skew < timedelta(0) or clock_skew >= _MAX_TTL:
            raise ValueError("clock_skew must be non-negative and below 60 seconds")
        self._clock_skew_seconds = int(clock_skew.total_seconds())
        self._replay_cache = replay_cache or AssertionReplayCache()

    def verify(  # noqa: PLR0913 - exact signed request binding stays explicit
        self,
        token: str,
        *,
        required_scopes: tuple[str, ...],
        request_id: str,
        request_method: str,
        request_path: str,
        now: datetime | None = None,
    ) -> IdentityClaims:
        """Verify a compact JWS and consume its replay identifier.

        Parameters
        ----------
        token : str
            Compact JWS from Platform.
        required_scopes : tuple[str, ...]
            Capabilities required by the destination route.
        request_id : str
            Request identifier that must match the signed claim.
        request_method : str
            HTTP method that must match the signed claim.
        request_path : str
            Absolute path that must match the signed claim.
        now : datetime or None, optional
            Verification time. Defaults to the current UTC time.

        Returns
        -------
        IdentityClaims
            Fully verified claims.

        Raises
        ------
        IdentityAssertionError
            If parsing, signature, key, issuer, audience, scope, time, request
            binding, or replay validation fails.
        """
        encoded_header, encoded_payload, encoded_signature = _compact_parts(token)
        header = _decode_json_object(encoded_header, expected_fields=_HEADER_FIELDS)
        if header.get("alg") != _ALGORITHM or header.get("typ") != _TOKEN_TYPE:
            raise IdentityAssertionError("identity assertion algorithm or type is invalid")
        key_id = _claim_text(header, "kid")
        key_record = self._keys.get(key_id)
        if key_record is None:
            raise IdentityAssertionError("identity assertion key is unknown")

        verified_at = _utc_datetime(now or datetime.now(UTC), "now")
        if key_record.accept_until is not None:
            accept_until = _utc_datetime(key_record.accept_until, "accept_until")
            if verified_at > accept_until:
                raise IdentityAssertionError("identity assertion key overlap has ended")

        signed = f"{encoded_header}.{encoded_payload}".encode()
        try:
            key_record.public_key.verify(_decode_bytes(encoded_signature), signed)
        except (InvalidSignature, ValueError) as exc:
            raise IdentityAssertionError("identity assertion signature is invalid") from exc

        payload = _decode_json_object(encoded_payload, expected_fields=_CLAIM_FIELDS)
        claims = _claims_from_payload(payload)
        expected_request_id = _required_text(request_id, "request_id")
        if claims.issuer != self._issuer:
            raise IdentityAssertionError("identity assertion issuer is invalid")
        if claims.audience != self._audience:
            raise IdentityAssertionError("identity assertion audience is invalid")
        if claims.request_id != expected_request_id:
            raise IdentityAssertionError("identity assertion request binding is invalid")
        if claims.request_method != _request_method(request_method):
            raise IdentityAssertionError("identity assertion method binding is invalid")
        if claims.request_path != _request_path(request_path):
            raise IdentityAssertionError("identity assertion path binding is invalid")

        required = set(_normalize_scopes(required_scopes))
        if not required.issubset(claims.scopes):
            raise IdentityAssertionError("identity assertion scope is insufficient")

        timestamp = int(verified_at.timestamp())
        skew = self._clock_skew_seconds
        if claims.expires_at <= claims.issued_at:
            raise IdentityAssertionError("identity assertion lifetime is invalid")
        if claims.not_before > claims.expires_at:
            raise IdentityAssertionError("identity assertion validity window is invalid")
        if claims.expires_at - claims.issued_at > int(_MAX_TTL.total_seconds()):
            raise IdentityAssertionError("identity assertion lifetime exceeds 60 seconds")
        if claims.not_before > timestamp + skew:
            raise IdentityAssertionError("identity assertion is not yet valid")
        if claims.issued_at > timestamp + skew:
            raise IdentityAssertionError("identity assertion issuance is in the future")
        if claims.expires_at < timestamp - skew:
            raise IdentityAssertionError("identity assertion has expired")
        self._replay_cache.consume(
            claims.token_id,
            expires_at=claims.expires_at + skew,
            now=timestamp,
        )
        return claims


def _claims_from_payload(payload: Mapping[str, Any]) -> IdentityClaims:
    if payload.get("ver") != _FORMAT_VERSION:
        raise IdentityAssertionError("identity assertion version is unsupported")
    scopes_value = payload.get("scp")
    if not isinstance(scopes_value, list) or not all(
        isinstance(item, str) for item in scopes_value
    ):
        raise IdentityAssertionError("identity assertion scopes are invalid")
    user_id = payload.get("uid")
    if user_id is not None and (
        isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
    ):
        raise IdentityAssertionError("identity assertion user ID is invalid")
    return IdentityClaims(
        issuer=_claim_text(payload, "iss"),
        audience=_claim_text(payload, "aud"),
        subject=_claim_text(payload, "sub"),
        principal=_claim_text(payload, "prn"),
        user_id=user_id,
        user_role=_claim_optional_text(payload, "role"),
        session_id=_claim_optional_text(payload, "sid"),
        request_id=_claim_text(payload, "rid"),
        request_method=_claim_method(payload),
        request_path=_claim_path(payload),
        scopes=_normalize_scopes(tuple(scopes_value), error_type=IdentityAssertionError),
        issued_at=_claim_timestamp(payload, "iat"),
        not_before=_claim_timestamp(payload, "nbf"),
        expires_at=_claim_timestamp(payload, "exp"),
        token_id=_claim_text(payload, "jti"),
    )


def _compact_parts(token: str) -> tuple[str, str, str]:
    if not isinstance(token, str):
        raise IdentityAssertionError("identity assertion must be text")
    if len(token) > _MAX_COMPACT_LENGTH:
        raise IdentityAssertionError("identity assertion compact form is too large")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise IdentityAssertionError("identity assertion compact form is invalid")
    return parts[0], parts[1], parts[2]


def _encode_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return _encode_bytes(encoded)


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    if not value or "=" in value:
        raise IdentityAssertionError("identity assertion base64url is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IdentityAssertionError("identity assertion base64url is invalid") from exc


def _decode_json_object(value: str, *, expected_fields: frozenset[str]) -> dict[str, Any]:
    def reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise IdentityAssertionError("identity assertion JSON contains duplicate fields")
            result[key] = item
        return result

    try:
        decoded = json.loads(_decode_bytes(value), object_pairs_hook=reject_duplicate_fields)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IdentityAssertionError("identity assertion JSON is invalid") from exc
    if not isinstance(decoded, dict) or set(decoded) != expected_fields:
        raise IdentityAssertionError("identity assertion fields are invalid")
    return decoded


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty text without surrounding whitespace")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _claim_text(payload: Mapping[str, Any], name: str) -> str:
    try:
        return _required_text(payload.get(name), name)
    except ValueError as exc:
        raise IdentityAssertionError(f"identity assertion claim {name!r} is invalid") from exc


def _claim_optional_text(payload: Mapping[str, Any], name: str) -> str | None:
    try:
        return _optional_text(payload.get(name), name)
    except ValueError as exc:
        raise IdentityAssertionError(f"identity assertion claim {name!r} is invalid") from exc


def _claim_timestamp(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IdentityAssertionError(f"identity assertion claim {name!r} is invalid")
    return value


def _request_method(value: object) -> str:
    method = _required_text(value, "request_method")
    if method != method.upper() or not method.isascii() or not method.isalpha():
        raise ValueError("request_method must contain uppercase ASCII letters")
    return method


def _request_path(value: object) -> str:
    path = _required_text(value, "request_path")
    if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
        raise ValueError("request_path must be an absolute path without query or fragment")
    return path


def _claim_method(payload: Mapping[str, Any]) -> str:
    try:
        return _request_method(payload.get("mth"))
    except ValueError as exc:
        raise IdentityAssertionError("identity assertion method claim is invalid") from exc


def _claim_path(payload: Mapping[str, Any]) -> str:
    try:
        return _request_path(payload.get("pth"))
    except ValueError as exc:
        raise IdentityAssertionError("identity assertion path claim is invalid") from exc


def _normalize_scopes(
    scopes: tuple[str, ...],
    *,
    error_type: type[ValueError] = ValueError,
) -> tuple[str, ...]:
    try:
        normalized = tuple(sorted({_required_text(scope, "scope") for scope in scopes}))
    except (TypeError, ValueError) as exc:
        raise error_type("identity assertion scopes are invalid") from exc
    if not normalized:
        raise error_type("identity assertion scopes must not be empty")
    return normalized


def _utc_datetime(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AssertionReplayCache",
    "IdentityAssertionError",
    "IdentityAssertionSigner",
    "IdentityAssertionVerifier",
    "IdentityClaims",
    "VerificationKey",
]
