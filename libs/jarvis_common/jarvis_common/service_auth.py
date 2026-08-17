"""Exact signed service-command authorization through Platform."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from jarvis_common.identity_capabilities import IdentityAudience, ServicePrincipal


class ServiceCommandUnavailableError(RuntimeError):
    """Raised when Platform cannot authorize an exact owner command."""


@dataclass(frozen=True, slots=True)
class ServiceCommand:
    """Exact destination and subject binding for one owner command."""

    audience: IdentityAudience
    method: str
    path: str
    user_id: int
    request_id: str | None = None


async def authorize_service_command(
    client: httpx.AsyncClient,
    *,
    platform_url: str,
    principal: ServicePrincipal,
    token: str,
    command: ServiceCommand,
) -> dict[str, str]:
    """Return headers for one Platform-authorized service command.

    The returned assertion is bound to the destination method, path, user, and
    request ID. Transport and payload failures are intentionally collapsed to
    a stable unavailable condition so callers never expose downstream details.
    """
    correlation_id = command.request_id or str(uuid.uuid4())
    try:
        response = await client.post(
            f"{platform_url.rstrip('/')}/internal/services/authorize",
            headers={
                "X-Jarvis-Service-Principal": principal,
                "X-Jarvis-Service-Token": token,
            },
            json={
                "audience": command.audience,
                "method": command.method,
                "path": command.path,
                "request_id": correlation_id,
                "user_id": command.user_id,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ServiceCommandUnavailableError(
            "service command authorization is unavailable"
        ) from exc
    assertion = payload.get("assertion")
    if not isinstance(assertion, str) or not assertion:
        raise ServiceCommandUnavailableError("service command authorization is unavailable")
    return {"X-Jarvis-Identity": assertion, "X-Request-Id": correlation_id}


__all__ = ["ServiceCommand", "ServiceCommandUnavailableError", "authorize_service_command"]
