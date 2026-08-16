"""Shared dependencies for the Platform API."""

from __future__ import annotations

from typing import Annotated, cast

import asyncpg
from fastapi import Depends, Header, HTTPException, Request
from jarvis_common import create_limiter
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.identity_capabilities import ServicePrincipal
from jarvis_common.settings import get_secrets_settings

from platform_api.service_principals import ServicePrincipalTokens

limiter = create_limiter()


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


def get_identity_signer(request: Request) -> IdentityAssertionSigner:
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


def get_configured_api_key() -> str:
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
]
