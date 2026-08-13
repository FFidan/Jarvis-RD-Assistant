"""Provider connectivity probe: test cloud LLM API keys via live HTTP."""

import httpx
from jarvis_common.maintenance import OutboundEgressBlockedError, ensure_outbound_egress_allowed
from jarvis_common.pinned_transport import LOCAL_DEVELOPMENT_POLICY, pinned_async_client
from pydantic import BaseModel

from paper_ingestion.services.llm_provider_registry import (
    CLOUD_PROVIDERS,
    validate_custom_openai_base_url_for_outbound,
)

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
    """Authenticate against Anthropic's model list, as every other provider does.

    The token-count endpoint this used to call needs a model id in the request
    body, so retiring that id would turn every key test into a failure that reads
    like a bad key. Listing models authenticates just as well and names nothing
    that can be retired out from under it.
    """
    return await client.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
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


def _result_from_provider_response(response: httpx.Response | None) -> ProviderTestResult:
    """Convert a provider probe response into a non-raising result."""
    if response is None:
        return ProviderTestResult(ok=False, error="unsupported provider")
    if response.is_success:
        return ProviderTestResult(ok=True)
    return ProviderTestResult(ok=False, error=f"provider returned HTTP {response.status_code}")


async def test_provider_connectivity(
    provider: str,
    api_key: str,
    *,
    base_url: str | None = None,
) -> ProviderTestResult:
    """Probe a cloud LLM provider with an operator-supplied API key.

    Parameters
    ----------
    provider : str
        Registered provider identifier.
    api_key : str
        Credential used only at the provider request boundary.
    base_url : str or None
        Validated custom OpenAI-compatible endpoint, when applicable.

    Returns
    -------
    ProviderTestResult
        Non-raising connectivity outcome for quarantine, validation, network,
        unsupported-provider, and provider HTTP failures.
    """
    try:
        ensure_outbound_egress_allowed("cloud provider connectivity probe")
    except OutboundEgressBlockedError:
        return ProviderTestResult(
            ok=False,
            error="provider access is disabled until restored credentials are reviewed",
        )

    if provider == "custom_openai_compatible" and base_url:
        try:
            await validate_custom_openai_base_url_for_outbound(base_url)
        except ValueError as exc:
            return ProviderTestResult(ok=False, error=str(exc))

    try:
        ensure_outbound_egress_allowed("cloud provider connectivity probe")
        async with pinned_async_client(
            LOCAL_DEVELOPMENT_POLICY, timeout=httpx.Timeout(10.0)
        ) as client:
            resp = await _probe_provider_models(client, provider, api_key, base_url=base_url)
    except OutboundEgressBlockedError:
        return ProviderTestResult(
            ok=False,
            error="provider access is disabled until restored credentials are reviewed",
        )
    except httpx.HTTPError:
        return ProviderTestResult(ok=False, error="provider request failed")

    return _result_from_provider_response(resp)
