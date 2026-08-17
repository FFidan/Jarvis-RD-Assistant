"""HTTPX authentication flow for scoped Telegram backend assertions."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
from jarvis_common.identity_capabilities import IdentityAudience, service_principal_scopes
from jarvis_common.telemetry import correlation_id, trace_headers

from telegram_bot.config import BotConfig


class TelegramBackendAuth(httpx.Auth):
    """Exchange paired-user context for a route-bound Platform assertion.

    Parameters
    ----------
    config : BotConfig
        Configured Research, Learning, and Platform service origins.
    platform_client : httpx.AsyncClient
        Dedicated client carrying Telegram's Platform service credential.
    """

    def __init__(self, config: BotConfig, platform_client: httpx.AsyncClient) -> None:
        self._config = config
        self._platform_client = platform_client

    async def async_auth_flow(
        self,
        request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Authorize and yield one exact downstream request.

        Parameters
        ----------
        request : httpx.Request
            Pending Research or Learning request containing a local paired-user
            marker.

        Yields
        ------
        httpx.Request
            Request carrying only the signed identity and request identifier.

        Raises
        ------
        httpx.HTTPStatusError
            If Platform refuses the service credential, user, or capability.
        RuntimeError
            If the destination is not an exact configured/allowlisted backend
            or no valid paired-user marker is present.
        """
        raw_user_id = request.headers.pop("X-Owner-User-Id", None)
        try:
            user_id = int(raw_user_id or "")
        except ValueError as exc:
            raise RuntimeError("Telegram backend requests require a paired user") from exc
        if user_id <= 0:
            raise RuntimeError("Telegram backend requests require a positive paired user")

        audience = self._resolve_audience(request)
        request_id = request.headers.get("X-Request-Id") or correlation_id() or str(uuid.uuid4())
        response = await self._platform_client.post(
            f"{self._config.platform_api_url}/internal/telegram/authorize",
            headers=trace_headers(),
            json={
                "audience": audience,
                "method": request.method,
                "path": request.url.path,
                "request_id": request_id,
                "user_id": user_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        assertion = payload.get("assertion")
        if not isinstance(assertion, str) or not assertion:
            raise RuntimeError("Platform returned an invalid Telegram assertion response")

        request.headers.pop("X-API-Key", None)
        request.headers["X-Jarvis-Identity"] = assertion
        request.headers["X-Request-Id"] = request_id
        request.headers["X-Correlation-Id"] = request_id
        request.headers.update(trace_headers())
        yield request

    def _resolve_audience(self, request: httpx.Request) -> IdentityAudience:
        request_origin = _origin(request.url)
        candidates: list[IdentityAudience] = []
        for audience, configured_url in (
            ("learning", self._config.learning_engine_url),
            ("research", self._config.paper_ingestion_url),
        ):
            if request_origin != _origin(httpx.URL(configured_url)):
                continue
            if (
                service_principal_scopes(
                    "telegram",
                    audience,
                    request.method,
                    request.url.path,
                )
                is not None
            ):
                candidates.append(audience)
        if len(candidates) != 1:
            raise RuntimeError("Telegram backend destination or capability is not allowlisted")
        return candidates[0]


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.host, port


__all__ = ["TelegramBackendAuth"]
