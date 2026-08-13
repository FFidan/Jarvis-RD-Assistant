"""LiteLLM admin-API HTTP primitives and typed deployment models (facade leaf of litellm_config)."""

import hashlib
import logging
from typing import Any

import httpx
from jarvis_common.llm_client import build_litellm_headers, get_litellm_config
from jarvis_common.maintenance import ensure_outbound_egress_allowed
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, pinned_async_client
from jarvis_common.settings import get_secrets_settings
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Logger identity is intentionally pinned to the parent module's name (not
# __name__) so this leaf's log records stay attributed to litellm_config.
logger = logging.getLogger("paper_ingestion.services.litellm_config")


_HTTP_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Typed deployment element models (validated at the boundary from /v1/model/info)
# ---------------------------------------------------------------------------


class LiteLLMModelInfo(BaseModel):
    """Subset of the model_info dict that callers actually read (YAGNI)."""

    model_config = ConfigDict(extra="ignore")
    id: str = ""
    db_model: bool = False


class LiteLLMDeployment(BaseModel):
    """Single element returned by GET /v1/model/info → data[]."""

    model_config = ConfigDict(extra="ignore")
    model_name: str  # required; absent = malformed (element is skipped)
    litellm_params: dict[str, Any] = Field(default_factory=dict)
    model_info: LiteLLMModelInfo = Field(default_factory=LiteLLMModelInfo)

    @field_validator("litellm_params", "model_info", mode="before")
    @classmethod
    def _null_to_default(cls, v: Any) -> Any:
        # LiteLLM can emit an explicit null for these; the old dict-based code
        # treated null as empty (entry.get(...) or {}). Coerce so a deployment
        # with a null model_info/litellm_params is kept, not dropped.
        return {} if v is None else v


def _parse_deployment(elem: Any) -> "LiteLLMDeployment | None":
    """Validate one raw deployment dict; log WARNING and return None if malformed."""
    try:
        return LiteLLMDeployment.model_validate(elem)
    except Exception as exc:
        entry_id = elem.get("model_name") if isinstance(elem, dict) else repr(elem)
        logger.warning("Skipping malformed LiteLLM deployment entry %r: %s", entry_id, exc)
        return None


def _key_fingerprint(api_key: str | None) -> str:
    """Short keyed, non-reversible identity for a delivered key ('' = no key).

    Uncached by design: caching would retain the plaintext key material in the
    cache keys, and at reconciler cadence the derivation cost is negligible.
    """
    if not api_key:
        return ""
    cfg = get_secrets_settings().jarvis_config_key
    secret = cfg.get_secret_value().encode() if cfg else b"jarvis-key-fingerprint"
    return hashlib.pbkdf2_hmac("sha256", api_key.encode(), secret, 10_000).hex()[:16]


def _redact_secret(text: str, secret: Any) -> str:
    """Strip *secret* from error text before it reaches logs / HTTP details."""
    if isinstance(secret, str) and secret:
        return text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# LiteLLM admin-API primitives
# ---------------------------------------------------------------------------


async def get_litellm_deployments() -> list[LiteLLMDeployment]:
    """Return all LiteLLM deployments via ``GET /v1/model/info``.

    Each entry is a validated ``LiteLLMDeployment`` (model_name, litellm_params,
    model_info). ``db_model`` is True for admin-DB deployments (deletable via
    ``/model/delete``) and False for YAML-seeded ones (NOT deletable at runtime).
    ``litellm_params.api_key`` is masked by LiteLLM and never round-trips.
    Malformed elements are logged as WARNING and skipped.

    Returns
    -------
    list[LiteLLMDeployment]
        Validated deployment records; malformed individual elements are skipped.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials remain quarantined.
    RuntimeError
        If the admin endpoint is unreachable, returns an error, or has an
        invalid top-level response shape.
    """
    ensure_outbound_egress_allowed("LiteLLM deployment listing")
    litellm_cfg = get_litellm_config()
    try:
        ensure_outbound_egress_allowed("LiteLLM deployment listing")
        async with pinned_async_client(JARVIS_SERVICE_POLICY, timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{litellm_cfg.base_url}/v1/model/info",
                headers=build_litellm_headers(litellm_cfg),
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"LiteLLM /v1/model/info unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LiteLLM /v1/model/info failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json().get("data")
    if not isinstance(data, list):
        raise RuntimeError("LiteLLM /v1/model/info returned an unexpected shape (no data list)")
    result: list[LiteLLMDeployment] = []
    for elem in data:
        if not isinstance(elem, dict):
            continue
        dep = _parse_deployment(elem)
        if dep is not None:
            result.append(dep)
    return result


async def _post_model_new(alias: str, litellm_params: dict[str, Any]) -> str | None:
    """Create a deployment for *alias* via ``POST /model/new``.

    Parameters
    ----------
    alias : str
        LiteLLM alias assigned to the deployment.
    litellm_params : dict[str, Any]
        Provider parameters; any API key is redacted from raised details.

    Returns
    -------
    str or None
        New deployment ID when LiteLLM reports one.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials remain quarantined.
    RuntimeError
        On transport or HTTP failure. The sanitized message retains enough
        response detail for callers to detect a missing admin database.
    """
    ensure_outbound_egress_allowed("LiteLLM deployment creation")
    litellm_cfg = get_litellm_config()
    payload = {"model_name": alias, "litellm_params": litellm_params}
    # The payload may carry a decrypted cloud api_key, and FastAPI 422s echo the
    # submitted body — any raised error text must be redacted or the plaintext
    # key flows into HTTPException details (browser), reconciler warning
    # tracebacks (docker logs -> Vector), and setup.py warnings.
    api_key = litellm_params.get("api_key")
    try:
        ensure_outbound_egress_allowed("LiteLLM deployment creation")
        async with pinned_async_client(JARVIS_SERVICE_POLICY, timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{litellm_cfg.base_url}/model/new",
                json=payload,
                headers=build_litellm_headers(litellm_cfg),
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"LiteLLM /model/new unreachable for alias {alias!r}: "
            f"{_redact_secret(str(exc), api_key)}"
        ) from exc
    if resp.status_code >= 400:
        # Redact BEFORE truncating — a key straddling the 500-char cut would
        # survive a truncate-then-replace.
        body_text = _redact_secret(resp.text, api_key)
        raise RuntimeError(
            f"LiteLLM /model/new failed for alias {alias!r}: "
            f"HTTP {resp.status_code} {body_text[:500]}"
        )
    try:
        body = resp.json()
    except ValueError:
        return None
    model_id = body.get("model_id") if isinstance(body, dict) else None
    return str(model_id) if model_id else None


async def _post_model_delete(deployment_id: str) -> None:
    """Delete a DB deployment by id via ``POST /model/delete``.

    Parameters
    ----------
    deployment_id : str
        Exact LiteLLM database deployment identifier.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials remain quarantined.
    RuntimeError
        On transport failure or an HTTP error response.
    """
    ensure_outbound_egress_allowed("LiteLLM deployment deletion")
    litellm_cfg = get_litellm_config()
    try:
        ensure_outbound_egress_allowed("LiteLLM deployment deletion")
        async with pinned_async_client(JARVIS_SERVICE_POLICY, timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{litellm_cfg.base_url}/model/delete",
                json={"id": deployment_id},
                headers=build_litellm_headers(litellm_cfg),
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"LiteLLM /model/delete unreachable for deployment {deployment_id!r}: {exc}"
        ) from exc
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LiteLLM /model/delete failed for deployment {deployment_id!r}: "
            f"HTTP {resp.status_code} {resp.text[:300]}"
        )
