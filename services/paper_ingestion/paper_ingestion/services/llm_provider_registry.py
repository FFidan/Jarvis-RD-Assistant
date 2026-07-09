"""Registry of supported LLM providers and their routing metadata."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

ProviderKind = Literal["direct", "router", "self_hosted"]
PrivacyBoundary = Literal["direct_provider", "router", "self_hosted"]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class ProviderDefinition:
    """Static metadata for one admin-wide LLM provider integration."""

    id: str
    display_name: str
    kind: ProviderKind
    api_key_config_key: str
    litellm_prefix: str
    env_var: str
    privacy_boundary: PrivacyBoundary
    best_for: str
    data_note: str
    base_url_config_key: str | None = None
    default_base_url: str | None = None
    delivery_prefix: str | None = None
    supports_assignment: bool = True

    @property
    def assignment_prefix(self) -> str:
        """Return the app-facing model prefix used in settings/model catalog ids."""
        return self.litellm_prefix

    @property
    def provider_model_prefix(self) -> str:
        """Return the LiteLLM model prefix sent to the proxy."""
        return self.delivery_prefix or self.litellm_prefix


PROVIDER_REGISTRY: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        id="anthropic",
        display_name="Anthropic Claude",
        kind="direct",
        api_key_config_key="llm.anthropic.api_key",
        litellm_prefix="anthropic/",
        env_var="ANTHROPIC_API_KEY",
        privacy_boundary="direct_provider",
        best_for="Careful long-context synthesis and writing.",
        data_note="Selected prompts and source excerpts are sent to Anthropic when assigned.",
    ),
    ProviderDefinition(
        id="openai",
        display_name="OpenAI",
        kind="direct",
        api_key_config_key="llm.openai.api_key",
        litellm_prefix="openai/",
        env_var="OPENAI_API_KEY",
        privacy_boundary="direct_provider",
        best_for="General reasoning, structured extraction, and synthesis.",
        data_note="Selected prompts and source excerpts are sent to OpenAI when assigned.",
    ),
    ProviderDefinition(
        id="google",
        display_name="Google Gemini",
        kind="direct",
        api_key_config_key="llm.google.api_key",
        litellm_prefix="gemini/",
        env_var="GEMINI_API_KEY",
        privacy_boundary="direct_provider",
        best_for="Multimodal-capable models and fast lower-cost tiers.",
        data_note="Selected prompts and source excerpts are sent to Google when assigned.",
    ),
    ProviderDefinition(
        id="openrouter",
        display_name="OpenRouter",
        kind="router",
        api_key_config_key="llm.providers.openrouter.api_key",
        litellm_prefix="openrouter/",
        env_var="OPENROUTER_API_KEY",
        privacy_boundary="router",
        best_for="Trying many hosted models through one router account.",
        data_note="Requests pass through OpenRouter and then the selected upstream provider.",
    ),
    ProviderDefinition(
        id="deepseek",
        display_name="DeepSeek",
        kind="direct",
        api_key_config_key="llm.providers.deepseek.api_key",
        litellm_prefix="deepseek/",
        env_var="DEEPSEEK_API_KEY",
        privacy_boundary="direct_provider",
        best_for="Cost-conscious reasoning and extraction candidates.",
        data_note="Selected prompts and source excerpts are sent to DeepSeek when assigned.",
    ),
    ProviderDefinition(
        id="mistral",
        display_name="Mistral AI",
        kind="direct",
        api_key_config_key="llm.providers.mistral.api_key",
        litellm_prefix="mistral/",
        env_var="MISTRAL_API_KEY",
        privacy_boundary="direct_provider",
        best_for="European provider option and efficient chat models.",
        data_note="Selected prompts and source excerpts are sent to Mistral when assigned.",
    ),
    ProviderDefinition(
        id="moonshot",
        display_name="Kimi / Moonshot",
        kind="direct",
        api_key_config_key="llm.providers.moonshot.api_key",
        litellm_prefix="moonshot/",
        env_var="MOONSHOT_API_KEY",
        privacy_boundary="direct_provider",
        best_for="Long-context Kimi models and multilingual synthesis.",
        data_note="Selected prompts and source excerpts are sent to Moonshot when assigned.",
    ),
    ProviderDefinition(
        id="zai",
        display_name="Z.ai / GLM",
        kind="direct",
        api_key_config_key="llm.providers.zai.api_key",
        litellm_prefix="zai/",
        env_var="ZAI_API_KEY",
        privacy_boundary="direct_provider",
        best_for="GLM long-context and reasoning-capable candidates.",
        data_note="Selected prompts and source excerpts are sent to Z.ai when assigned.",
    ),
    ProviderDefinition(
        id="custom_openai_compatible",
        display_name="Custom OpenAI-compatible endpoint",
        kind="self_hosted",
        api_key_config_key="llm.providers.custom_openai_compatible.api_key",
        base_url_config_key="llm.providers.custom_openai_compatible.base_url",
        litellm_prefix="custom_openai/",
        delivery_prefix="openai/",
        env_var="CUSTOM_OPENAI_API_KEY",
        privacy_boundary="self_hosted",
        best_for="Self-hosted vLLM, institutional gateways, or compatible endpoints.",
        data_note="Requests are sent to the configured endpoint. Verify its operator and logs.",
        default_base_url=None,
    ),
)

PROVIDERS_BY_ID: dict[str, ProviderDefinition] = {
    provider.id: provider for provider in PROVIDER_REGISTRY
}
PROVIDERS_BY_PREFIX: dict[str, ProviderDefinition] = {
    provider.assignment_prefix.rstrip("/"): provider for provider in PROVIDER_REGISTRY
}
CLOUD_PROVIDERS: frozenset[str] = frozenset(PROVIDERS_BY_ID)
CLOUD_MODEL_PREFIXES: tuple[str, ...] = tuple(
    provider.assignment_prefix for provider in PROVIDER_REGISTRY
)
PROVIDER_API_KEY_CONFIG_KEYS: frozenset[str] = frozenset(
    provider.api_key_config_key for provider in PROVIDER_REGISTRY
)
PROVIDER_BASE_URL_CONFIG_KEYS: frozenset[str] = frozenset(
    provider.base_url_config_key
    for provider in PROVIDER_REGISTRY
    if provider.base_url_config_key is not None
)
PROVIDER_CONFIG_KEYS: frozenset[str] = PROVIDER_API_KEY_CONFIG_KEYS | PROVIDER_BASE_URL_CONFIG_KEYS


def provider_for_id(provider_id: str) -> ProviderDefinition:
    """Return provider metadata for *provider_id* or raise ``ValueError``."""
    try:
        return PROVIDERS_BY_ID[provider_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported provider {provider_id!r}") from exc


def provider_for_prefix(prefix: str) -> ProviderDefinition | None:
    """Return provider metadata for an app-facing model prefix."""
    return PROVIDERS_BY_PREFIX.get(prefix.rstrip("/"))


def provider_model_for_delivery(provider: ProviderDefinition, model_suffix: str) -> str:
    """Return the LiteLLM model string for *provider* and app-facing suffix."""
    return f"{provider.provider_model_prefix}{model_suffix}"


def validate_custom_openai_base_url(value: str) -> None:
    """Validate an admin-supplied OpenAI-compatible endpoint base URL.

    The endpoint may be HTTPS on any host or HTTP on loopback only. User-info,
    fragments, missing hosts, invalid schemes, and metadata-service IPs are
    rejected before the URL can be stored.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("custom endpoint base URL must be a non-empty string")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("custom endpoint base URL must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ValueError("custom endpoint base URL must not include credentials")
    if parsed.fragment:
        raise ValueError("custom endpoint base URL must not include a fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("custom endpoint base URL has an invalid port") from exc

    hostname = parsed.hostname
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip is not None and _blocked_custom_endpoint_ip(ip):
        raise ValueError("custom endpoint base URL uses a blocked network address")

    is_loopback = hostname in {"localhost"} or (ip is not None and ip.is_loopback)
    if parsed.scheme == "http" and not is_loopback:
        raise ValueError("plain HTTP custom endpoints must be loopback-only")


# RFC 6598 carrier-grade NAT shared address space is not flagged by
# ``ipaddress.is_private`` on all supported interpreters, so match it explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _blocked_custom_endpoint_ip(ip: IPAddress) -> bool:
    """Return True for resolved addresses custom provider endpoints must not use.

    Loopback is deliberately excluded so the loopback-only HTTP dev carve-out in
    :func:`validate_custom_openai_base_url` keeps working (``is_private`` covers
    ``127.0.0.0/8`` and ``::1``).
    """

    return (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip == ipaddress.ip_address("169.254.169.254")
        or ((ip.is_private or ip.is_reserved or ip in _CGNAT_NETWORK) and not ip.is_loopback)
    )


def _ip_literal(hostname: str) -> IPAddress | None:
    """Parse *hostname* as an IP literal, returning None for DNS names."""

    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


async def validate_custom_openai_base_url_for_outbound(value: str) -> None:
    """Resolve and validate a custom OpenAI-compatible endpoint before outbound use."""

    validate_custom_openai_base_url(value)
    parsed = urlparse(value.strip())
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("custom endpoint base URL must include a host")

    literal_ip = _ip_literal(hostname)
    explicit_loopback = hostname == "localhost" or (
        literal_ip is not None and literal_ip.is_loopback
    )
    if literal_ip is not None:
        resolved_ips = {literal_ip}
    else:
        loop = asyncio.get_running_loop()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = await loop.run_in_executor(
                None, socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise ValueError("custom endpoint host could not be resolved") from exc
        resolved_ips = set()
        for info in infos:
            address = info[4][0]
            try:
                resolved_ips.add(ipaddress.ip_address(address))
            except ValueError:
                continue

    if not resolved_ips:
        raise ValueError("custom endpoint host could not be resolved")
    for ip in resolved_ips:
        if _blocked_custom_endpoint_ip(ip) or (ip.is_loopback and not explicit_loopback):
            raise ValueError("custom endpoint resolves to a blocked network address")
