"""Provider connectivity probe: test cloud LLM API keys via live HTTP."""

import httpx
from pydantic import BaseModel

from paper_ingestion.services.config_metadata import CLOUD_PROVIDERS

__all__ = [
    "ProviderTestResult",
    "_SUPPORTED_PROVIDERS",
    "test_provider_connectivity",
]


class ProviderTestResult(BaseModel):
    ok: bool
    error: str | None = None


_SUPPORTED_PROVIDERS = CLOUD_PROVIDERS


async def test_provider_connectivity(
    provider: str,
    api_key: str,
) -> ProviderTestResult:
    """Probe a cloud LLM provider with *api_key* to verify connectivity.

    Returns a :class:`ProviderTestResult` — never raises.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            if provider == "anthropic":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    json={
                        "model": "claude-sonnet-4-5",
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                )
            elif provider == "openai":
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            else:  # google
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                )
    except httpx.HTTPError as exc:
        return ProviderTestResult(ok=False, error=str(exc)[:200])

    if resp.is_success:
        return ProviderTestResult(ok=True)
    return ProviderTestResult(ok=False, error=f"provider returned HTTP {resp.status_code}")
