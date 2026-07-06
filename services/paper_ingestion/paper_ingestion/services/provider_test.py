"""Provider connectivity probe: test cloud LLM API keys via live HTTP."""

import httpx
from pydantic import BaseModel

from paper_ingestion.services.llm_provider_registry import CLOUD_PROVIDERS

__all__ = [
    "ProviderTestResult",
    "_SUPPORTED_PROVIDERS",
    "test_provider_connectivity",
]


class ProviderTestResult(BaseModel):
    """Non-raising result returned by provider connectivity probes."""

    ok: bool
    error: str | None = None


_SUPPORTED_PROVIDERS = CLOUD_PROVIDERS
_OPENAI_COMPATIBLE_MODEL_URLS = {
    "openai": "https://api.openai.com/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "deepseek": "https://api.deepseek.com/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "moonshot": "https://api.moonshot.ai/v1/models",
    "zai": "https://api.z.ai/api/paas/v4/models",
}


def _bearer_headers(api_key: str) -> dict[str, str]:
    """Return standard bearer-auth headers for model-list probes."""

    return {"Authorization": f"Bearer {api_key}"}


async def _probe_anthropic(client: httpx.AsyncClient, api_key: str) -> httpx.Response:
    """Use Anthropic's token-count endpoint as a low-cost credential probe."""

    return await client.post(
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


async def _probe_provider_models(
    client: httpx.AsyncClient,
    provider: str,
    api_key: str,
    *,
    base_url: str | None,
) -> httpx.Response | None:
    """Dispatch provider model-list probes, returning ``None`` when unsupported."""

    if provider == "anthropic":
        return await _probe_anthropic(client, api_key)
    if provider == "google":
        return await client.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
        )
    if provider == "custom_openai_compatible":
        if not base_url:
            return None
        return await client.get(
            f"{base_url.rstrip('/')}/models",
            headers=_bearer_headers(api_key),
        )
    if models_url := _OPENAI_COMPATIBLE_MODEL_URLS.get(provider):
        return await client.get(models_url, headers=_bearer_headers(api_key))
    return None


async def test_provider_connectivity(
    provider: str,
    api_key: str,
    *,
    base_url: str | None = None,
) -> ProviderTestResult:
    """Probe a cloud LLM provider with *api_key* to verify connectivity.

    Returns a :class:`ProviderTestResult` and never raises for network or
    provider HTTP failures.
    """

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await _probe_provider_models(client, provider, api_key, base_url=base_url)
    except httpx.HTTPError as exc:
        return ProviderTestResult(ok=False, error=str(exc)[:200])

    if resp is None:
        return ProviderTestResult(ok=False, error="unsupported provider")
    if resp.is_success:
        return ProviderTestResult(ok=True)
    return ProviderTestResult(ok=False, error=f"provider returned HTTP {resp.status_code}")
