"""Trusted gateway subrequest endpoint for backend identity assertions."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from jarvis_common.auth import RAW_CLIENT_SCOPE_KEY, api_key_matches
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.identity_capabilities import IdentityAudience, required_identity_scopes

from platform_api.config import PlatformSettings, get_platform_settings
from platform_api.deps import get_configured_api_key, get_identity_signer

router = APIRouter(prefix="/internal", tags=["internal"])


async def _platform_settings_dependency() -> PlatformSettings:
    """Return cached Platform settings without a thread-pool handoff.

    Returns
    -------
    PlatformSettings
        Process-wide gateway and identity configuration.
    """
    return get_platform_settings()


@dataclass(frozen=True, slots=True)
class _AuthorizationRuntime:
    """Cached process dependencies required to mint one assertion."""

    signer: IdentityAssertionSigner
    configured_api_key: str
    settings: PlatformSettings


async def _authorization_runtime(request: Request) -> _AuthorizationRuntime:
    """Resolve non-blocking authorization dependencies in one task.

    Parameters
    ----------
    request : Request
        Request whose application state owns the signing key.

    Returns
    -------
    _AuthorizationRuntime
        Signer, operations key, and cached gateway settings.
    """
    return _AuthorizationRuntime(
        signer=await get_identity_signer(request),
        configured_api_key=await get_configured_api_key(),
        settings=await _platform_settings_dependency(),
    )


@router.get("/authorize", status_code=status.HTTP_204_NO_CONTENT)
async def authorize_backend_request(  # noqa: PLR0913 - exact gateway headers and dependencies
    request: Request,
    audience: Annotated[IdentityAudience, Header(alias="X-Jarvis-Target-Audience")],
    original_method: Annotated[str, Header(alias="X-Jarvis-Original-Method")],
    original_path: Annotated[str, Header(alias="X-Jarvis-Original-Path")],
    request_id: Annotated[str, Header(alias="X-Request-Id")],
    runtime: Annotated[_AuthorizationRuntime, Depends(_authorization_runtime)],
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Response:
    """Authorize one gateway request and return a signed backend identity.

    Parameters
    ----------
    request : Request
        Internal subrequest carrying validated session state and the stashed
        raw transport peer.
    audience : {"learning", "research"}
        Exact destination backend.
    original_method : str
        External request method to bind into the assertion.
    original_path : str
        External request path to bind into the assertion.
    request_id : str
        Gateway request identifier to bind into the assertion.
    api_key : str or None, optional
        Operations API key when the request does not carry a browser session.
    runtime : _AuthorizationRuntime
        Cached signer, operations key, and gateway configuration.

    Returns
    -------
    Response
        Empty 204 response carrying ``X-Jarvis-Identity``.

    Raises
    ------
    HTTPException
        With status 400 for an unsupported backend route, 401 when no accepted
        identity is present, or 403 when the raw caller is not the gateway.
    """
    if not _gateway_peer_allowed(request, runtime.settings):
        raise HTTPException(status_code=403, detail="Gateway authorization is forbidden")

    method = original_method
    try:
        scopes = required_identity_scopes(audience, method, original_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Backend request binding is invalid") from exc
    if scopes is None:
        raise HTTPException(
            status_code=400, detail="Backend route does not use identity assertions"
        )

    user_id = getattr(request.state, "user_id", None)
    user_role = getattr(request.state, "user_role", None)
    session_id = getattr(request.state, "session_id", None)
    if isinstance(user_id, int) and not isinstance(user_id, bool) and isinstance(session_id, str):
        principal = "browser"
        subject = f"user:{user_id}"
    elif runtime.configured_api_key and api_key_matches(api_key, runtime.configured_api_key):
        principal = "api-key"
        subject = "operator-api-key"
        user_id = None
        user_role = None
        session_id = None
    else:
        raise HTTPException(status_code=401, detail="Authentication required")

    assertion = runtime.signer.issue(
        audience=audience,
        subject=subject,
        principal=principal,
        user_id=user_id,
        user_role=user_role if isinstance(user_role, str) else None,
        session_id=session_id,
        request_id=request_id,
        request_method=method,
        request_path=original_path,
        scopes=scopes,
    )
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Jarvis-Identity": assertion},
    )


def _gateway_peer_allowed(request: Request, settings: PlatformSettings) -> bool:
    raw_peer = request.scope.get(RAW_CLIENT_SCOPE_KEY)
    if not isinstance(raw_peer, tuple | list) or not raw_peer or not isinstance(raw_peer[0], str):
        return False
    try:
        address = ipaddress.ip_address(raw_peer[0])
    except ValueError:
        return False
    return any(address in network for network in settings.gateway_auth_networks)


__all__ = ["authorize_backend_request", "router"]
