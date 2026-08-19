"""Shared dependencies for the Platform API."""

from __future__ import annotations

from typing import Annotated, cast

import asyncpg
from fastapi import Depends, Header, HTTPException, Request
from jarvis_common import create_limiter
from jarvis_common.auth import verify_api_key
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.identity_capabilities import ServicePrincipal
from jarvis_common.settings import get_secrets_settings

from platform_api.service_principals import ServicePrincipalTokens

limiter = create_limiter()

_SERVICE_AUTHENTICATED_PATHS = frozenset(
    {
        "/internal/services/authorize",
        "/internal/services/research-config-effects",
    }
)
_SELF_AUTHENTICATED_PATHS = frozenset({"/internal/authorize"})


def _uses_service_authentication(path: str) -> bool:
    """Return whether a Platform path has its own service-principal contract."""
    return path in _SERVICE_AUTHENTICATED_PATHS or path.startswith("/internal/telegram/")


def get_db_pool(request: Request) -> asyncpg.Pool:
    """Return the Platform database pool.

    Parameters
    ----------
    request : Request
        Request whose app lifespan created ``app.state.db_pool``.

    Returns
    -------
    asyncpg.Pool
        Platform runtime connection pool.
    """
    return request.app.state.db_pool


async def get_identity_signer(request: Request) -> IdentityAssertionSigner:
    """Return the Platform-only assertion signer.

    Parameters
    ----------
    request : Request
        Request whose app lifespan loaded ``app.state.identity_signer``.

    Returns
    -------
    IdentityAssertionSigner
        Configured Ed25519 signer.

    Raises
    ------
    HTTPException
        With status 503 when key loading did not complete.
    """
    signer = getattr(request.app.state, "identity_signer", None)
    if not isinstance(signer, IdentityAssertionSigner):
        raise HTTPException(status_code=503, detail="Identity signing is unavailable")
    return signer


def get_service_principal_tokens(request: Request) -> ServicePrincipalTokens:
    """Return service-principal credentials loaded during startup.

    Parameters
    ----------
    request : Request
        Request whose application state owns the credential snapshot.

    Returns
    -------
    ServicePrincipalTokens
        Validated service credentials.

    Raises
    ------
    HTTPException
        With status 503 when credential loading did not complete.
    """
    tokens = getattr(request.app.state, "service_principal_tokens", None)
    if not isinstance(tokens, ServicePrincipalTokens):
        raise HTTPException(status_code=503, detail="Service authentication is unavailable")
    return tokens


def authenticate_service_principal(
    principal: Annotated[str, Header(alias="X-Jarvis-Service-Principal")],
    token: Annotated[str, Header(alias="X-Jarvis-Service-Token")],
    configured: ServicePrincipalTokens = Depends(get_service_principal_tokens),
) -> ServicePrincipal:
    """Authenticate one internal service using its dedicated credential.

    Parameters
    ----------
    principal : str
        Claimed service identity header.
    token : str
        Presented service credential header.
    configured : ServicePrincipalTokens
        Startup-loaded credential snapshot.

    Returns
    -------
    {"learning", "research", "telegram"}
        Authenticated service identity.

    Raises
    ------
    HTTPException
        With status 401 for an unknown principal or invalid credential.
    """
    if principal not in {"learning", "research", "telegram"}:
        raise HTTPException(status_code=401, detail="Service authentication failed")
    typed_principal = cast(ServicePrincipal, principal)
    if not configured.authenticates(typed_principal, token):
        raise HTTPException(status_code=401, detail="Service authentication failed")
    return typed_principal


async def verify_platform_request(
    request: Request,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    principal: Annotated[str | None, Header(alias="X-Jarvis-Service-Principal")] = None,
    token: Annotated[str | None, Header(alias="X-Jarvis-Service-Token")] = None,
) -> None:
    """Apply the authentication contract owned by the requested Platform route.

    Parameters
    ----------
    request : Request
        Incoming Platform request.
    api_key : str or None, optional
        General operations key for browser and operator routes.
    principal : str or None, optional
        Dedicated service identity for exact internal routes.
    token : str or None, optional
        Dedicated service credential paired with ``principal``.

    Raises
    ------
    HTTPException
        When the route's general or service-specific authentication fails.

    Notes
    -----
    The gateway authorization endpoint enforces its peer, session or API-key,
    and request-binding contract itself, so repeating the general API-key
    dependency would add work without adding an independent check.

    Service-authenticated routes still declare their route-local dependency;
    this application dependency prevents the unrelated general API-key check
    from rejecting them first.
    """
    if request.url.path in _SELF_AUTHENTICATED_PATHS:
        return
    if _uses_service_authentication(request.url.path):
        if principal is None or token is None:
            raise HTTPException(status_code=401, detail="Service authentication failed")
        authenticate_service_principal(
            principal,
            token,
            get_service_principal_tokens(request),
        )
        return
    await verify_api_key(request, api_key)


async def get_configured_api_key() -> str:
    """Return the configured operations API key without logging it.

    Returns
    -------
    str
        Plain key value, or an empty string when no key is configured.
    """
    secret = get_secrets_settings().jarvis_api_key
    return secret.get_secret_value() if secret is not None else ""


__all__ = [
    "authenticate_service_principal",
    "get_configured_api_key",
    "get_db_pool",
    "get_identity_signer",
    "get_service_principal_tokens",
    "limiter",
    "verify_platform_request",
]
