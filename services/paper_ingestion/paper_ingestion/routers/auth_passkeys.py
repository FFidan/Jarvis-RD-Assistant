"""WebAuthn passkey sign-in ceremonies and per-user credential management.

Endpoints (prefix ``/api/auth/passkeys``):

- ``POST register/begin``  — session-required; issue registration options.
- ``POST register/finish`` — session-required; verify + store a new credential.
- ``POST login/begin``     — unauthenticated; issue discoverable-credential options.
- ``POST login/finish``    — unauthenticated; verify an assertion and mint a session.
- ``GET  capability``      — unauthenticated; can this origin do passkeys here?
- ``GET  ""``              — session-required; list the caller's own credentials.
- ``DELETE /{credential_id}`` — session-required; delete the caller's own credential.

Front-door exemption: ``verify_api_key`` returns early for every ``/api/auth/``
path (see ``jarvis_common.auth.verify_api_key``), so the whole router bypasses the
global API-key gate. The unauthenticated ceremonies (login/*, capability) rely on
that alone; the session-required ceremonies additionally enforce identity in-handler
via :func:`current_user_id_strict`, taking ``user_id`` from the session cookie and
NEVER from the request body (registering a credential is account-scoped).

Origin/rp_id are matched against a server-side allowlist rather than derived from
proxy headers: Caddy rewrites Host→localhost and does not forward a trustworthy
X-Forwarded-Host, so request.url/Host would be attacker-influenceable. See
:func:`_match_origin`.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import urlparse

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status
from jarvis_common.audit import log_audit
from jarvis_common.auth import current_user_id_strict
from jarvis_common.session_middleware import mint_session
from pydantic import BaseModel
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    options_to_json_dict,
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import limiter
from paper_ingestion.routers.auth import UserResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/passkeys", tags=["auth-passkeys"])
# Exempt from the global verify_api_key dep — enforced by the /api/auth/ path
# check in verify_api_key. Marker attribute so future linters can audit.
router.auth_exempt = True  # type: ignore[attr-defined]

RP_NAME = "JARVIS"
_CHALLENGE_TTL_SQL = "now() + INTERVAL '5 minutes'"

# Claim-before-verify: the DELETE is the read. A single-use nonce — exactly one
# concurrent finish can claim a given challenge; a replay/expired/wrong-purpose
# finish gets no row. Registration additionally checks the returned user_id.
_CONSUME_CHALLENGE_SQL = (
    "DELETE FROM webauthn_challenges "
    "WHERE challenge = $1 AND purpose = $2 AND expires_at > now() "
    "RETURNING user_id"
)

_INSERT_CHALLENGE_SQL = (
    "INSERT INTO webauthn_challenges (challenge, user_id, purpose, expires_at) "
    f"VALUES ($1, $2, $3, {_CHALLENGE_TTL_SQL})"
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PasskeyCapability(BaseModel):
    available: bool
    access_mode: str


class PasskeyRegistrationResult(BaseModel):
    id: str
    nickname: str | None = None


class PasskeyInfo(BaseModel):
    id: str
    nickname: str | None = None
    transports: list[str] | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None


# ---------------------------------------------------------------------------
# Origin allowlist — never derive rp_id/origin from proxy headers
# ---------------------------------------------------------------------------


class _OriginMatch(NamedTuple):
    origin: str
    rp_id: str


# Loopback secure-context hostnames the app is served under. RFC 6761 reserves the
# ``.localhost`` TLD for client loopback, so a page on either of these is reachable
# only from the user's own machine — never from an attacker's network. Each is a
# valid effective domain (rp_id) on any port, so they match on ANY port (the local
# dashboard is served on localhost:<DASHBOARD_HOST_PORT>, e.g. :3001). The default
# installer serves plain http here, which W3C Secure Contexts still treats as a
# trustworthy context for WebAuthn (see ``_origin_allowed``). The set is exactly these
# two hosts (not arbitrary ``*.localhost``) — the pair the stack actually serves.
_LOOPBACK_RP_IDS = frozenset({"localhost", "jarvis.localhost"})

# Default port per scheme, stripped when normalising an origin so an explicit
# ``:443`` in APP_BASE_URL still matches the browser's port-less Origin.
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _app_base_origin() -> str | None:
    """The configured public origin (``scheme://host[:port]``, no path), or None.

    Sourced from ``APP_BASE_URL`` (written by setup for tunnel/Let's-Encrypt modes).
    The scheme's default port is stripped so ``https://host:443`` and ``https://host``
    are one origin. A malformed port makes the whole value unusable (None), fail-closed.
    """
    base = get_paper_ingestion_settings().app_base_url
    if not base:
        return None
    parsed = urlparse(base)
    try:
        parsed_port = parsed.port
    except ValueError:
        return None
    # A PUBLIC (non-loopback) WebAuthn origin MUST be https — reject a misconfigured
    # http:// APP_BASE_URL rather than advertise an origin browsers block. (Loopback
    # http is trustworthy and is accepted separately in ``_origin_allowed``.)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    port = (
        f":{parsed_port}"
        if parsed_port and parsed_port != _DEFAULT_PORTS.get(parsed.scheme)
        else ""
    )
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _origin_allowed(origin: str | None) -> _OriginMatch | None:
    """Resolve a browser ``Origin`` to a permitted ``(origin, rp_id)``, else None.

    The two loopback secure-context hosts (``localhost``, ``jarvis.localhost``) are
    accepted on http OR https and on ANY port — loopback is not attacker-reachable and
    their rp_id is valid for every port. A PUBLIC origin must EXACTLY equal the configured
    ``APP_BASE_URL`` origin (no suffix/port relaxation for reachable hosts). A raw-IP
    origin such as ``https://127.0.0.1`` is never accepted — an IP is an invalid rp_id.
    """
    if not origin:
        return None
    parsed = urlparse(origin)
    host = parsed.hostname
    # W3C Secure Contexts treats http://localhost (and the reserved *.localhost TLD) as
    # potentially trustworthy, so browsers permit WebAuthn there — accept the two loopback
    # hosts on http OR https. Public hosts and raw IPs stay https-only / rejected below.
    if parsed.scheme in ("https", "http") and host in _LOOPBACK_RP_IDS:
        return _OriginMatch(origin=origin, rp_id=host)
    base = _app_base_origin()
    if base and origin == base and host:
        return _OriginMatch(origin=origin, rp_id=host)
    return None


def _match_origin(request: Request) -> _OriginMatch:
    """Match the browser ``Origin`` header against the allowlist.

    Returns ``(origin, rp_id)`` where ``rp_id`` is the matched origin's hostname.
    Raises 403 when the Origin is absent or not allowlisted (the ceremony cannot
    proceed with an rp_id we cannot trust).
    """
    match = _origin_allowed(request.headers.get("origin"))
    if match is None:
        raise HTTPException(status_code=403, detail="Origin not permitted for passkey ceremony")
    return match


def _is_clone(stored_count: int, new_count: int) -> bool:
    """WebAuthn L3 signature-counter clone test.

    A non-incrementing counter is a clone/replay signal; ``0/0`` is normal for
    counter-less or cloud-synced authenticators (never flagged).
    """
    return (new_count != 0 or stored_count != 0) and new_count <= stored_count


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.post("/register/begin")
@limiter.limit("10/minute")
async def passkey_register_begin(request: Request) -> dict[str, Any]:
    """Issue registration options for the session user (UV + resident key required)."""
    uid = await current_user_id_strict(request)
    match = _match_origin(request)
    user_name = getattr(request.state, "user_email", None) or f"user-{uid}"
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        existing = await conn.fetch(
            "SELECT credential_id FROM webauthn_credentials WHERE user_id = $1", uid
        )
        options = generate_registration_options(
            rp_id=match.rp_id,
            rp_name=RP_NAME,
            user_id=uid.to_bytes(8, "big"),
            user_name=user_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=bytes(row["credential_id"])) for row in existing
            ],
        )
        await conn.execute(_INSERT_CHALLENGE_SQL, options.challenge, uid, "register")
    return options_to_json_dict(options)


@router.post("/register/finish", response_model=PasskeyRegistrationResult)
@limiter.limit("10/minute")
async def passkey_register_finish(
    request: Request, payload: dict[str, Any]
) -> PasskeyRegistrationResult:
    """Verify a registration response and store the new credential for the session user."""
    uid = await current_user_id_strict(request)
    match = _match_origin(request)
    raw_nick = payload.get("nickname")
    nickname = raw_nick.strip()[:64] if isinstance(raw_nick, str) and raw_nick.strip() else None
    try:
        reg_cred = parse_registration_credential_json(payload)
        challenge = parse_client_data_json(reg_cred.response.client_data_json).challenge
    except Exception as exc:  # noqa: BLE001 — any parse failure is a client error
        raise HTTPException(status_code=400, detail="Malformed registration credential") from exc

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        claimed = await conn.fetchrow(_CONSUME_CHALLENGE_SQL, challenge, "register")
        if claimed is None or claimed["user_id"] is None or int(claimed["user_id"]) != uid:
            raise HTTPException(status_code=400, detail="Invalid or expired registration challenge")
        try:
            verified = verify_registration_response(
                credential=reg_cred,
                expected_challenge=challenge,
                expected_rp_id=match.rp_id,
                expected_origin=match.origin,
                require_user_verification=True,
            )
        except Exception as exc:  # noqa: BLE001 — verification failure is a client error
            raise HTTPException(status_code=400, detail="Registration verification failed") from exc
        try:
            credential_id = await _store_credential(conn, uid, verified, reg_cred, nickname)
        except asyncpg.UniqueViolationError as exc:
            # The credential_id UNIQUE constraint already blocks reuse; surface it as a
            # clean 409 (this authenticator is registered here, possibly to another account)
            # instead of a 500.
            raise HTTPException(
                status_code=409, detail="This passkey is already registered"
            ) from exc

    await log_audit(
        pool,
        action="auth.passkey.register",
        resource=f"webauthn_credentials/{credential_id}",
        user_id=str(uid),
    )
    return PasskeyRegistrationResult(id=str(credential_id), nickname=nickname)


async def _store_credential(
    conn: Any, user_id: int, verified: Any, reg_cred: Any, nickname: str | None
) -> uuid.UUID:
    """Insert a verified registration into ``webauthn_credentials``; return its id."""
    transports = [t.value for t in (reg_cred.response.transports or [])] or None
    return await conn.fetchval(
        "INSERT INTO webauthn_credentials "
        "(user_id, credential_id, public_key, sign_count, transports, aaguid, nickname) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        user_id,
        verified.credential_id,
        verified.credential_public_key,
        verified.sign_count,
        transports,
        uuid.UUID(verified.aaguid),
        nickname,
    )


# ---------------------------------------------------------------------------
# Login (discoverable / username-less)
# ---------------------------------------------------------------------------


@router.post("/login/begin")
@limiter.limit("10/minute")
async def passkey_login_begin(request: Request) -> dict[str, Any]:
    """Issue discoverable-credential authentication options (UV required)."""
    match = _match_origin(request)
    options = generate_authentication_options(
        rp_id=match.rp_id, user_verification=UserVerificationRequirement.REQUIRED
    )
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        await conn.execute(_INSERT_CHALLENGE_SQL, options.challenge, None, "login")
    return options_to_json_dict(options)


@router.post("/login/finish", response_model=UserResponse)
@limiter.limit("10/minute")
async def passkey_login_finish(
    request: Request, response: Response, payload: dict[str, Any]
) -> UserResponse:
    """Verify an assertion and mint a session cookie for the resolved user."""
    match = _match_origin(request)
    try:
        auth_cred = parse_authentication_credential_json(payload)
        challenge = parse_client_data_json(auth_cred.response.client_data_json).challenge
    except Exception as exc:  # noqa: BLE001 — any parse failure is a client error
        raise HTTPException(status_code=400, detail="Malformed authentication credential") from exc

    pool = request.app.state.db_pool
    now = datetime.now(UTC)
    async with pool.acquire() as conn:
        claimed = await conn.fetchrow(_CONSUME_CHALLENGE_SQL, challenge, "login")
        if claimed is None:
            raise HTTPException(status_code=401, detail="Authentication failed")
        user_id, credential_id = await _authenticate_assertion(
            conn, pool, auth_cred, challenge, match
        )
        user_row = await conn.fetchrow(
            "SELECT email, role, deleted_at FROM users WHERE id = $1", user_id
        )
        if user_row is None or user_row["deleted_at"] is not None:
            raise HTTPException(status_code=401, detail="Authentication failed")
        async with conn.transaction():
            await mint_session(conn, response, user_id, now=now, credential_id=credential_id)

    await log_audit(
        pool, action="auth.passkey.login", resource="webauthn_credentials", user_id=str(user_id)
    )
    return UserResponse(id=user_id, email=user_row["email"], role=user_row["role"])


async def _authenticate_assertion(
    conn: Any, pool: Any, auth_cred: Any, challenge: bytes, match: _OriginMatch
) -> tuple[int, uuid.UUID]:
    """Verify the assertion, apply the counter policy, and return ``(user_id, credential_id)``.

    The library's internal signature-counter check is disabled
    (``credential_current_sign_count=0``) so verification runs the SIGNATURE check
    and returns ``new_sign_count``; the clone policy is then applied against the
    stored count. Doing it this way gates clone-detection on a valid signature — a
    forged low-counter request without the private key fails verification and is
    rejected as a plain auth failure, never as a (credential-revoking) clone.

    On a clone the credential and its sessions are revoked in-transaction; the
    revocation is committed before the 401 is raised so it persists despite the
    rejection.
    """
    clone = False
    async with conn.transaction():
        cred = await conn.fetchrow(
            "SELECT id, user_id, public_key, sign_count FROM webauthn_credentials "
            "WHERE credential_id = $1 FOR UPDATE",
            auth_cred.raw_id,
        )
        if cred is None:
            raise HTTPException(status_code=401, detail="Authentication failed")
        try:
            verified = verify_authentication_response(
                credential=auth_cred,
                expected_challenge=challenge,
                expected_rp_id=match.rp_id,
                expected_origin=match.origin,
                credential_public_key=bytes(cred["public_key"]),
                credential_current_sign_count=0,
                require_user_verification=True,
            )
        except Exception as exc:  # noqa: BLE001 — verification failure is an auth failure
            raise HTTPException(status_code=401, detail="Authentication failed") from exc
        if _is_clone(cred["sign_count"], verified.new_sign_count):
            await _revoke_credential_and_sessions(conn, cred["id"])
            clone = True
        else:
            await conn.execute(
                "UPDATE webauthn_credentials SET sign_count = $1, last_used_at = now() "
                "WHERE id = $2",
                verified.new_sign_count,
                cred["id"],
            )
    if clone:
        await log_audit(
            pool,
            action="auth.passkey.clone_detected",
            resource=f"webauthn_credentials/{cred['id']}",
            user_id=str(cred["user_id"]),
        )
        raise HTTPException(status_code=401, detail="Authentication failed")
    return int(cred["user_id"]), cred["id"]


async def _revoke_credential_and_sessions(conn: Any, credential_id: uuid.UUID) -> None:
    """Revoke the credential's sessions, then delete it (same transaction).

    Sessions are revoked BEFORE the delete so the ``credential_id`` FK link still
    resolves; magic-link/api-key sessions (``credential_id IS NULL``) are untouched.
    """
    await conn.execute(
        "UPDATE sessions SET revoked_at = now() WHERE credential_id = $1 AND revoked_at IS NULL",
        credential_id,
    )
    await conn.execute("DELETE FROM webauthn_credentials WHERE id = $1", credential_id)


# ---------------------------------------------------------------------------
# Capability + credential management
# ---------------------------------------------------------------------------


@router.post("/capability", response_model=PasskeyCapability)
@limiter.limit("30/minute")
async def passkey_capability(request: Request) -> PasskeyCapability:
    """Report whether this origin can run passkey ceremonies, and the access mode.

    Lets the frontend explain WHY passkeys are/aren't offered here without a
    feature flag: ``available`` is purely the current Origin ∈ allowlist. This is a
    POST (not GET) because browsers OMIT the Origin header on a same-origin GET, so a
    GET probe would always see origin=None and report ``available=False`` in production.
    """
    return PasskeyCapability(
        available=_origin_allowed(request.headers.get("origin")) is not None,
        access_mode=os.environ.get("JARVIS_ACCESS_MODE", "localhost"),
    )


@router.get("", response_model=list[PasskeyInfo])
@limiter.limit("30/minute")
async def list_passkeys(request: Request) -> list[PasskeyInfo]:
    """List the caller's OWN registered credentials (never another user's)."""
    uid = await current_user_id_strict(request)
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        "SELECT id, nickname, transports, created_at, last_used_at "
        "FROM webauthn_credentials WHERE user_id = $1 ORDER BY created_at",
        uid,
    )
    return [
        PasskeyInfo(
            id=str(row["id"]),
            nickname=row["nickname"],
            transports=row["transports"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )
        for row in rows
    ]


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def delete_passkey(request: Request, credential_id: str) -> Response:
    """Delete the caller's OWN credential and revoke the sessions it minted."""
    uid = await current_user_id_strict(request)
    try:
        cred_uuid = uuid.UUID(credential_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Passkey not found") from exc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            owned = await conn.fetchval(
                "SELECT id FROM webauthn_credentials WHERE id = $1 AND user_id = $2 FOR UPDATE",
                cred_uuid,
                uid,
            )
            if owned is None:
                raise HTTPException(status_code=404, detail="Passkey not found")
            await _revoke_credential_and_sessions(conn, cred_uuid)
    await log_audit(
        pool,
        action="auth.passkey.delete",
        resource=f"webauthn_credentials/{cred_uuid}",
        user_id=str(uid),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
