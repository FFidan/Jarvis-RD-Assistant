"""Model catalog, hardware probing, status helpers, and pull job."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import socket
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

import httpx
from jarvis_common.model_catalog import (
    ModelCatalogEntry,
    Role,
    load_model_catalog,
    warn_if_catalog_stale,
)

logger = logging.getLogger(__name__)

Status = Literal["active", "pulled", "downloadable", "unfit", "cloud_active", "cloud_required"]
Fit = Literal["recommended", "stretch", "available", "key_required", "unfit"]


class FitDetail(TypedDict):
    """VRAM-fit breakdown for a single model at a requested context length.

    Parameters
    ----------
    default:
        Categorical fit verdict: ``"fits"``, ``"partial"``, ``"unfit"``,
        ``"cloud"``, or ``"unknown"``.
    at_num_ctx:
        The ``num_ctx`` value for which the fit was computed.
    required_vram_gb:
        Estimated VRAM requirement in GB at ``at_num_ctx``, or ``None`` when indeterminate.
    base_vram_gb:
        Estimated VRAM requirement in GB at ``base_num_ctx``, used as the what-if baseline.
    base_num_ctx:
        Context length corresponding to ``base_vram_gb``.
    default_num_ctx:
        Catalog default context length used as the VRAM baseline.
    max_num_ctx:
        Maximum supported context length for this entry.
    kv_cache_bytes_per_token:
        KV-cache memory cost per token in bytes, or ``None`` when unknown.
    """

    default: Literal["fits", "partial", "unfit", "cloud", "unknown"]
    at_num_ctx: int
    required_vram_gb: float | None
    base_vram_gb: float | None
    base_num_ctx: int
    default_num_ctx: int
    max_num_ctx: int
    kv_cache_bytes_per_token: int | None


class ModelStatusDict(TypedDict):
    """Combined model status entry returned by :func:`build_model_statuses`.

    All keys are required: every item produced by :func:`build_model_statuses`
    contains both the base catalog fields (from ``ModelCatalogEntry.to_dict()``)
    and the runtime-computed fields listed below.
    """

    # -- base keys from ModelCatalogEntry.to_dict() / asdict() --
    id: str
    name: str
    provider: str
    ollama_tag: str | None
    roles: tuple[str, ...]
    vram_gb: float
    disk_gb: float
    context_tokens: int
    license: str
    tier: int
    description: str
    notes: str
    last_reviewed: str
    embedding_dimension: int | None
    phase: str
    assignable: bool
    min_vram_gb_at_default_ctx: float | None
    kv_cache_bytes_per_token: int | None
    default_num_ctx: int | None
    max_num_ctx: int | None
    supports_thinking: bool
    # -- runtime keys added by build_model_statuses --
    active: bool
    pulled: bool
    provider_key_present: bool | None
    fit: Fit
    status: Status
    can_assign: bool
    assign_blocker: str | None
    fit_detail: FitDetail


MODEL_CATALOG: tuple[ModelCatalogEntry, ...] = load_model_catalog()
warn_if_catalog_stale(MODEL_CATALOG)

_HARDWARE_TTL = timedelta(hours=1)


async def _raise_if_cancelled(ctx: Any) -> None:
    is_cancelled = getattr(ctx, "is_cancelled", None)
    if is_cancelled is None:
        return
    cancelled = is_cancelled()
    if hasattr(cancelled, "__await__"):
        cancelled = await cancelled
    if cancelled:
        raise RuntimeError("model.pull cancelled")


@dataclass(frozen=True)
class HardwareInfo:
    """Immutable snapshot of the detected GPU/CPU memory configuration.

    Attributes
    ----------
    vram_gb : float
        Total available VRAM in gigabytes (0.0 for CPU-only machines).
    vram_source : str
        Detection method: ``"nvidia-smi"``, ``"macos-approx"``, or ``"cpu"``.
    tier : int
        Hardware capability tier (0=CPU, 1=4–10 GB, 2=10–20 GB, 3=20–40 GB, 4=40+ GB).
    detected_at : str
        ISO 8601 UTC timestamp of when the hardware was probed.
    machine_id : str
        Hostname of the machine, for diagnostics.
    """

    vram_gb: float
    vram_source: Literal["nvidia-smi", "macos-approx", "cpu"]
    tier: int
    detected_at: str
    machine_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict representation of this hardware snapshot."""
        return asdict(self)


def normalize_model_tag(tag: str) -> str:
    """Normalize implicit ``latest`` suffixes and LiteLLM provider prefixes.

    Parameters
    ----------
    tag : str
        Raw model tag as returned by Ollama or the LiteLLM config, e.g.
        ``"ollama/qwen3:8b"`` or ``"mistral-nemo:latest"``.

    Returns
    -------
    str
        Canonical tag with the ``ollama/`` prefix and ``:latest`` suffix
        stripped (e.g. ``"qwen3:8b"`` or ``"mistral-nemo"``).
    """
    value = tag.strip()
    if value.startswith("ollama/"):
        value = value.removeprefix("ollama/")
    return value.removesuffix(":latest")


def hardware_tier(vram_gb: float) -> int:
    """Map a VRAM value (GB) to a hardware capability tier (0–4).

    Parameters
    ----------
    vram_gb : float
        Available VRAM in gigabytes.

    Returns
    -------
    int
        Tier level: 0=CPU/no-GPU, 1=4–10 GB, 2=10–20 GB, 3=20–40 GB, 4=40+ GB.
    """
    if vram_gb >= 40:
        return 4
    if vram_gb >= 20:
        return 3
    if vram_gb >= 10:
        return 2
    if vram_gb >= 4:
        return 1
    return 0


def _probe_nvidia_smi() -> float | None:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return None

    values: list[float] = []
    for line in proc.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            values.append(float(text) / 1024.0)
        except ValueError:
            continue
    return max(values) if values else None


def _probe_macos_vram() -> float | None:
    if platform.system().lower() != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except Exception:
        return None
    for line in proc.stdout.splitlines():
        if "VRAM" not in line:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*(MB|GB)", line, re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1))
        return value / 1024.0 if match.group(2).upper() == "MB" else value
    return None


def detect_hardware() -> HardwareInfo:
    """Probe local accelerator memory without shell expansion."""
    vram = _probe_nvidia_smi()
    source: Literal["nvidia-smi", "macos-approx", "cpu"] = "nvidia-smi"
    if vram is None:
        vram = _probe_macos_vram()
        source = "macos-approx"
    if vram is None:
        vram = 0.0
        source = "cpu"
    rounded = round(float(vram), 1)
    return HardwareInfo(
        vram_gb=rounded,
        vram_source=source,
        tier=hardware_tier(rounded),
        detected_at=datetime.now(UTC).isoformat(),
        machine_id=socket.gethostname(),
    )


def get_cached_hardware(state: Any | None = None) -> HardwareInfo:
    """Return cached hardware info from app state, refreshing once per hour."""
    now = datetime.now(UTC)
    cached = getattr(state, "hw_info", None) if state is not None else None
    cached_at = getattr(state, "hw_info_at", None) if state is not None else None
    if isinstance(cached, HardwareInfo) and isinstance(cached_at, datetime):
        if now - cached_at < _HARDWARE_TTL:
            return cached

    info = detect_hardware()
    if state is not None:
        state.hw_info = info
        state.hw_info_at = now
    return info


async def async_get_cached_hardware(state: Any | None = None) -> HardwareInfo:
    """Async variant of get_cached_hardware for use in async handlers.

    Returns cached hardware when still fresh; otherwise runs detect_hardware()
    in a thread pool via asyncio.to_thread so the event loop is not blocked
    during the ~2 s nvidia-smi subprocess call.
    """
    now = datetime.now(UTC)
    cached = getattr(state, "hw_info", None) if state is not None else None
    cached_at = getattr(state, "hw_info_at", None) if state is not None else None
    if isinstance(cached, HardwareInfo) and isinstance(cached_at, datetime):
        if now - cached_at < _HARDWARE_TTL:
            return cached

    info = await asyncio.to_thread(detect_hardware)
    if state is not None:
        state.hw_info = info
        state.hw_info_at = now
    return info


def _installed_by_name(installed: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for item in installed:
        name = normalize_model_tag(str(item.get("name", "")))
        if name:
            by_name[name] = item
    return by_name


def _active_model_ids(current: dict[str, Any], embedding_model_name: str) -> set[str]:
    active = {normalize_model_tag(str(value)) for value in current.values() if value}
    active.add(normalize_model_tag(embedding_model_name))
    return active


def catalog_entry_for_model(model_id: str) -> ModelCatalogEntry | None:
    """Look up a catalog entry by model ID or Ollama tag.

    Parameters
    ----------
    model_id : str
        Model identifier to look up. Matched against both ``entry.id`` and
        ``entry.ollama_tag`` after normalization (strips ``ollama/`` prefix
        and ``:latest`` suffix).

    Returns
    -------
    ModelCatalogEntry | None
        The matching catalog entry, or ``None`` if no entry matches.
    """
    normalized = normalize_model_tag(model_id)
    for entry in MODEL_CATALOG:
        if normalize_model_tag(entry.id) == normalized:
            return entry
        if entry.ollama_tag and normalize_model_tag(entry.ollama_tag) == normalized:
            return entry
    return None


def cloud_provider_for_model(model_id: str) -> str | None:
    """Return the cloud provider name for a model, or ``None`` for local Ollama models.

    Parameters
    ----------
    model_id : str
        Model identifier (e.g. ``"anthropic/claude-3-5-sonnet"`` or ``"qwen3:8b"``).

    Returns
    -------
    str | None
        Provider string (``"anthropic"``, ``"openai"``, ``"google"``) when the
        model has a known cloud provider; ``None`` for local Ollama models.
    """
    entry = catalog_entry_for_model(model_id)
    if entry is not None and entry.provider != "ollama":
        return entry.provider
    if "/" not in model_id:
        return None
    prefix = model_id.split("/", 1)[0]
    if prefix in {"anthropic", "openai", "google", "gemini"}:
        return "google" if prefix == "gemini" else prefix
    return None


def _fit_for_entry(
    entry: ModelCatalogEntry,
    *,
    hardware: HardwareInfo,
    cloud_key_present: bool,
) -> Fit:
    if entry.provider != "ollama":
        return "available" if cloud_key_present else "key_required"
    if entry.tier <= hardware.tier:
        return "recommended"
    if entry.tier == hardware.tier + 1:
        return "stretch"
    return "unfit"


# Default KV cache marginal cost per token (bytes).  Conservatively ~1 KB/token
# covers most FP16 KV cache configurations. Catalog entries may override this
# with a measured value via the kv_cache_bytes_per_token field.
_DEFAULT_KV_CACHE_BYTES_PER_TOKEN = 1024

# Default context window when catalog entry has no default_num_ctx set.
_DEFAULT_NUM_CTX_FALLBACK = 8192


def compute_vram_fit(
    entry: ModelCatalogEntry,
    num_ctx: int,
    hardware: HardwareInfo,
) -> FitDetail:
    """Compute VRAM fit for an entry at a given num_ctx on this machine.

    Returns a dict matching contract §5.1 fit_detail shape:
      {"default": "fits"|"partial"|"unfit"|"cloud"|"unknown",
       "at_num_ctx": int, "required_vram_gb": float | None,
       "base_vram_gb": float | None, "base_num_ctx": int,
       "default_num_ctx": int, "max_num_ctx": int,
       "kv_cache_bytes_per_token": int | None}
    """
    # Resolve catalog-derived defaults (same values the frontend uses for what-if)
    resolved_default_ctx = (
        entry.default_num_ctx
        if entry.default_num_ctx is not None
        else min(_DEFAULT_NUM_CTX_FALLBACK, entry.context_tokens)
    )
    resolved_max_ctx = entry.max_num_ctx if entry.max_num_ctx is not None else entry.context_tokens
    resolved_kv = (
        entry.kv_cache_bytes_per_token
        if entry.kv_cache_bytes_per_token is not None
        else _DEFAULT_KV_CACHE_BYTES_PER_TOKEN
    )

    # Cloud models: skip math entirely
    if entry.provider != "ollama":
        return {
            "default": "cloud",
            "at_num_ctx": num_ctx,
            "required_vram_gb": None,
            "base_vram_gb": None,
            "base_num_ctx": resolved_default_ctx,
            "default_num_ctx": resolved_default_ctx,
            "max_num_ctx": resolved_max_ctx,
            "kv_cache_bytes_per_token": entry.kv_cache_bytes_per_token,
        }

    # Resolve base VRAM: prefer min_vram_gb_at_default_ctx, fall back to vram_gb
    min_vram = (
        entry.min_vram_gb_at_default_ctx
        if entry.min_vram_gb_at_default_ctx is not None
        else entry.vram_gb
    )

    # Hardware probe failed or CPU-only: publish the catalog baseline, but do not
    # claim a selected-context requirement or fit verdict for this machine.
    if hardware.vram_gb == 0.0:
        return {
            "default": "unknown",
            "at_num_ctx": num_ctx,
            "required_vram_gb": None,
            "base_vram_gb": round(min_vram, 3),
            "base_num_ctx": resolved_default_ctx,
            "default_num_ctx": resolved_default_ctx,
            "max_num_ctx": resolved_max_ctx,
            "kv_cache_bytes_per_token": entry.kv_cache_bytes_per_token,
        }

    # VRAM required at requested num_ctx
    extra_tokens = max(0, num_ctx - resolved_default_ctx)
    required_vram_gb = min_vram + extra_tokens * resolved_kv / 1e9

    vram_85 = hardware.vram_gb * 0.85
    vram_120 = hardware.vram_gb * 1.20

    if required_vram_gb <= vram_85:
        status = "fits"
    elif required_vram_gb <= vram_120:
        status = "partial"
    else:
        status = "unfit"

    return {
        "default": status,
        "at_num_ctx": num_ctx,
        "required_vram_gb": round(required_vram_gb, 3),
        "base_vram_gb": round(min_vram, 3),
        "base_num_ctx": resolved_default_ctx,
        "default_num_ctx": resolved_default_ctx,
        "max_num_ctx": resolved_max_ctx,
        "kv_cache_bytes_per_token": entry.kv_cache_bytes_per_token,
    }


def build_model_statuses(
    *,
    installed: list[dict[str, Any]],
    current: dict[str, Any],
    embedding_model_name: str,
    hardware: HardwareInfo | None = None,
    cloud_api_keys: dict[str, bool] | None = None,
    num_ctx_per_role: dict[str, int] | None = None,
) -> list[ModelStatusDict]:
    """Combine catalog, installed Ollama models, active assignments, and hardware.

    Parameters
    ----------
    num_ctx_per_role:
        Per-role num_ctx overrides keyed by role name (``"smart"``, ``"fast"``,
        ``"embed"``).  When provided, ``fit_detail`` is computed at the
        user's chosen context length; absent roles fall back to the catalog
        default.  Pass ``None`` (default) to always use catalog defaults.
    """
    hw = hardware or detect_hardware()
    cloud_keys = cloud_api_keys or {}
    ctx_per_role = num_ctx_per_role or {}
    installed_names = _installed_by_name(installed)
    active_ids = _active_model_ids(current, embedding_model_name)

    statuses: list[ModelStatusDict] = []
    for entry in MODEL_CATALOG:
        payload = entry.to_dict()
        active = normalize_model_tag(entry.id) in active_ids or (
            entry.ollama_tag is not None and normalize_model_tag(entry.ollama_tag) in active_ids
        )
        provider_key_present = bool(cloud_keys.get(entry.provider, False))
        fit = _fit_for_entry(entry, hardware=hw, cloud_key_present=provider_key_present)

        if entry.provider == "ollama":
            tag = normalize_model_tag(entry.ollama_tag or entry.id)
            installed_payload = installed_names.get(tag)
            pulled = installed_payload is not None
            if active:
                status: Status = "active"
            elif pulled:
                status = "pulled"
            elif fit == "unfit":
                status = "unfit"
            else:
                status = "downloadable"
            payload.update(installed_payload or {})
        else:
            pulled = False
            status = "cloud_active" if active and provider_key_present else "cloud_required"

        if not entry.assignable:
            can_assign = False
            assign_blocker = (
                entry.notes or "This model is tracked for evaluation but is not assignable yet."
            )
        elif entry.provider == "ollama":
            can_assign = pulled or active
            if can_assign:
                assign_blocker = None
            elif status == "unfit":
                assign_blocker = "Model does not fit this machine."
            else:
                assign_blocker = "Pull this model first."
        else:
            can_assign = provider_key_present or active
            assign_blocker = (
                None
                if can_assign
                else (f"Configure the {entry.provider} API key before assigning this model.")
            )

        # Determine the effective num_ctx for fit calculation.  Use the
        # most permissive (max) user-specified value across all roles this
        # entry supports; fall back to the catalog default when absent.
        role_ctxs = [ctx_per_role[r] for r in entry.roles if r in ctx_per_role]
        effective_num_ctx: int
        if role_ctxs:
            effective_num_ctx = max(role_ctxs)
        else:
            effective_num_ctx = (
                entry.default_num_ctx
                if entry.default_num_ctx is not None
                else min(_DEFAULT_NUM_CTX_FALLBACK, entry.context_tokens)
            )

        fit_detail = compute_vram_fit(entry, effective_num_ctx, hw)

        payload.update(
            {
                "active": active,
                "pulled": pulled,
                "provider_key_present": (
                    provider_key_present if entry.provider != "ollama" else None
                ),
                "fit": fit,
                "status": status,
                "can_assign": can_assign,
                "assign_blocker": assign_blocker,
                "fit_detail": fit_detail,
            }
        )
        statuses.append(payload)
    return statuses


def recommendations_for_role(
    role: Role,
    *,
    installed: list[dict[str, Any]],
    current: dict[str, Any],
    embedding_model_name: str,
    hardware: HardwareInfo | None = None,
    cloud_api_keys: dict[str, bool] | None = None,
) -> list[ModelStatusDict]:
    """Return catalog entries for one role, ranked by fit and readiness.

    Parameters
    ----------
    role : Role
        Target role (e.g. ``"smart"``, ``"fast"``, ``"embed"``).
    installed : list[dict]
        Ollama ``/api/tags`` response data (``[{"name": "...", ...}]``).
    current : dict
        Current model assignments from user config (e.g.
        ``{"smart": "qwen3:8b", "fast": "qwen3:4b", "embed": "nomic-embed-text"}``).
    embedding_model_name : str
        Canonical embedding model name from settings (used to mark it active
        regardless of the ``current`` dict).
    hardware : HardwareInfo | None
        Probed hardware info.  Detected fresh when ``None``.
    cloud_api_keys : dict[str, bool] | None
        Mapping of provider name → key-present flag.  Empty dict assumed when ``None``.

    Returns
    -------
    list[ModelStatusDict]
        Entries supporting *role* sorted by status priority then tier then name.
    """
    priority = {
        "active": 0,
        "pulled": 1,
        "downloadable": 2,
        "cloud_active": 1,
        "cloud_required": 3,
        "unfit": 4,
    }
    entries = [
        item
        for item in build_model_statuses(
            installed=installed,
            current=current,
            embedding_model_name=embedding_model_name,
            hardware=hardware,
            cloud_api_keys=cloud_api_keys,
        )
        if role in item["roles"]
    ]
    entries.sort(
        key=lambda item: (
            priority.get(str(item["status"]), 99),
            int(item["tier"]),
            item["name"],
        )
    )
    return entries


async def model_assignment_error(
    *,
    model_id: str,
    installed: list[dict[str, Any]],
    cloud_api_keys: dict[str, bool],
) -> str | None:
    """Return a user-facing assignment error, or None if assignment is allowed."""
    entry = catalog_entry_for_model(model_id)
    if entry is None:
        return f"Model {model_id!r} is not in the curated model catalog."
    if not entry.assignable:
        return entry.notes or "This model is tracked for evaluation but is not assignable yet."
    if entry.provider == "ollama":
        tag = normalize_model_tag(entry.ollama_tag or entry.id)
        if tag not in _installed_by_name(installed):
            return "Model not pulled. Pull it first."
        return None
    if not cloud_api_keys.get(entry.provider, False):
        return f"Configure the {entry.provider} API key before assigning this model."
    return None


async def _model_pull_job(
    pool: Any,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: Any,
) -> dict[str, Any]:
    """Pull an Ollama model while reporting progress to the job stream."""
    del pool
    tag = str(payload.get("ollama_tag") or payload.get("tag") or "")
    if not tag:
        raise ValueError("model.pull requires ollama_tag")
    entry = catalog_entry_for_model(tag)
    if entry is None or entry.provider != "ollama":
        raise ValueError(f"Model {tag!r} is not a local Ollama catalog entry")

    await _raise_if_cancelled(ctx)
    await ctx.update_progress(0.0, f"Starting pull for {tag}")
    ollama_url = str(payload.get("ollama_url") or "http://ollama:11434").rstrip("/")
    last_message = "Pulling model"
    try:
        async with http_client.stream(
            "POST",
            f"{ollama_url}/api/pull",
            json={"name": entry.ollama_tag or entry.id, "stream": True},
            timeout=None,
        ) as resp:
            if resp.status_code >= 400:
                text = await resp.aread()
                raise RuntimeError(f"Ollama model pull failed ({resp.status_code}): {text[:200]!r}")
            async for line in resp.aiter_lines():
                await _raise_if_cancelled(ctx)
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = str(event.get("status") or last_message)
                last_message = status
                total = event.get("total")
                completed = event.get("completed")
                if (
                    isinstance(total, int | float)
                    and total > 0
                    and isinstance(completed, int | float)
                ):
                    progress = min(0.99, max(0.01, float(completed) / float(total)))
                    await ctx.update_progress(progress, status)
                elif event.get("status"):
                    await ctx.update_progress(0.05, status)
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))
    except httpx.HTTPError as exc:
        await ctx.update_progress(0.0, f"Failed: Could not reach Ollama pull API: {exc}")
        raise RuntimeError(f"Could not reach Ollama pull API: {exc}") from exc
    except RuntimeError as exc:
        await ctx.update_progress(0.0, f"Failed: {exc}")
        raise

    await _raise_if_cancelled(ctx)
    await ctx.update_progress(1.0, "Done")
    return {"tag": entry.ollama_tag or entry.id, "status": "pulled", "message": last_message}
