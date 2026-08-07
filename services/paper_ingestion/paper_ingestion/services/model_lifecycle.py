"""Model catalog, hardware probing, status helpers, and pull job."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

import httpx
from jarvis_common.hw_detect import Vendor, vendor_from_env
from jarvis_common.maintenance import ensure_outbound_egress_allowed
from jarvis_common.model_catalog import (
    ModelCatalogEntry,
    Role,
    load_model_catalog,
)

from paper_ingestion.services.llm_provider_registry import provider_for_id
from paper_ingestion.services.model_prefixes import strip_ollama_prefix

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
        Detection method: ``"nvidia-smi"``, ``"macos-approx"``, ``"cpu"``,
        or ``"host-env"``.
    tier : int
        Hardware capability tier (0=CPU, 1=4–10 GB, 2=10–20 GB, 3=20–40 GB, 4=40+ GB).
    detected_at : str
        ISO 8601 UTC timestamp of when the hardware was probed.
    machine_id : str
        Hostname of the machine, for diagnostics.
    vram_source_detail : str
        Human-readable explanation of how VRAM was detected.
    host_gpu_divergence : bool
        True when JARVIS_HOST_VRAM_MB is set but the in-container GPU probe
        finds no GPU — signals that the GPU compose overlay is not active.
        Never set on AMD/Intel hosts: the in-container probe is nvidia-only,
        so its blindness there is structural, not a missing overlay.
    vendor : Vendor
        GPU vendor: the setup-written JARVIS_GPU_VENDOR when present, else
        inferred from the in-container probe (nvidia or none).
    """

    vram_gb: float
    vram_source: Literal["nvidia-smi", "macos-approx", "cpu", "host-env"]
    tier: int
    detected_at: str
    machine_id: str = ""
    vram_source_detail: str = ""
    host_gpu_divergence: bool = False
    vendor: Vendor = "none"

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
        Canonical tag with the ``ollama/`` / ``ollama_chat/`` prefix and
        ``:latest`` suffix stripped (e.g. ``"qwen3:8b"`` or ``"mistral-nemo"``).
    """
    value = strip_ollama_prefix(tag.strip())
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


_SOURCE_DETAIL: dict[str, str] = {
    "host-env": "GPU detected on the host at install time",
    "nvidia-smi": "GPU detected inside the container",
    "macos-approx": "estimated from Apple Silicon unified memory",
    "cpu": "no GPU detected — running on CPU",
}


def detect_hardware() -> HardwareInfo:
    """Probe local accelerator memory without shell expansion.

    When JARVIS_HOST_VRAM_MB is set to a positive integer, it overrides the
    probe result so the in-container hardware report reflects the host GPU even
    when the GPU compose overlay is not active.  The in-container probe still
    runs to detect whether the overlay is missing (host_gpu_divergence=True).
    """
    # In-container probe always runs (needed for divergence detection).
    container_vram = _probe_nvidia_smi()

    env_vendor = vendor_from_env()
    vendor: Vendor = env_vendor or ("nvidia" if container_vram is not None else "none")

    # Try JARVIS_HOST_VRAM_MB env override.
    host_env_gb: float | None = None
    raw_env = os.environ.get("JARVIS_HOST_VRAM_MB", "")
    if raw_env:
        try:
            mb = int(raw_env)
            if mb > 0:
                host_env_gb = mb / 1024.0
            else:
                # Parses but non-positive — an operator fat-finger; trace it so
                # the resulting CPU-tier behavior is not silent.
                logger.warning(
                    "JARVIS_HOST_VRAM_MB=%r is not a positive integer — ignored", raw_env
                )
        except ValueError:
            # Garbage value — ignore, fall through to normal probe, but trace it.
            logger.warning("JARVIS_HOST_VRAM_MB=%r is not a positive integer — ignored", raw_env)

    if host_env_gb is not None:
        source: Literal["nvidia-smi", "macos-approx", "cpu", "host-env"] = "host-env"
        vram = host_env_gb
        # Divergence: host claims GPU but the container probe found none. The
        # probe is nvidia-only, so it is structurally blind on AMD/Intel hosts
        # — only flag vendors it could have seen (a missing JARVIS_GPU_VENDOR,
        # i.e. a pre-vendor .env, keeps the original always-flag behavior).
        divergence = container_vram is None and env_vendor not in ("amd", "intel")
    else:
        divergence = False
        if container_vram is not None:
            vram = container_vram
            source = "nvidia-smi"
        else:
            macos_vram = _probe_macos_vram()
            if macos_vram is not None:
                vram = macos_vram
                source = "macos-approx"
            else:
                vram = 0.0
                source = "cpu"

    rounded = round(float(vram), 1)
    return HardwareInfo(
        vram_gb=rounded,
        vram_source=source,
        tier=hardware_tier(rounded),
        detected_at=datetime.now(UTC).isoformat(),
        machine_id=socket.gethostname(),
        vram_source_detail=_SOURCE_DETAIL[source],
        host_gpu_divergence=divergence,
        vendor=vendor,
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

# Co-residency headroom for the embed-reserve fit check
# (``fits_with_embed_reserve``): model + always-resident embedder must fit in
# 80% of VRAM. Stricter than ``compute_vram_fit``'s single-model 85%/120%
# thresholds because the catalog numbers don't model two-model overhead
# (CUDA contexts, allocator fragmentation): qwen3:14b + the 4b embedder on a
# 16 GB card sat at 81% nominal and still starved the embedder in practice —
# that incident is this constant's calibration point.
_CORESIDENCY_HEADROOM_FRACTION = 0.80


def compute_vram_fit(
    entry: ModelCatalogEntry,
    num_ctx: int,
    hardware: HardwareInfo,
) -> FitDetail:
    """Compute VRAM fit for an entry at a given num_ctx on this machine.

    Returns a dict matching the fit_detail shape:
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


# Must mirror the frontend slider stops (IngestionSection.tsx NUM_CTX_STOPS) —
# auto-seeded values have to land on a position the UI can render.
NUM_CTX_LADDER: tuple[int, ...] = (2048, 4096, 8192, 16384, 32768, 65536)


def _resolved_default_num_ctx(entry: ModelCatalogEntry) -> int:
    """Catalog default context length (same resolution as compute_vram_fit)."""
    if entry.default_num_ctx is not None:
        return entry.default_num_ctx
    return min(_DEFAULT_NUM_CTX_FALLBACK, entry.context_tokens)


def fits_with_embed_reserve(
    entry: ModelCatalogEntry,
    hardware: HardwareInfo,
    embed_reserve_gb: float,
    num_ctx: int | None = None,
) -> bool:
    """Return True when *entry* at *num_ctx* fits on this GPU beside the embed model.

    Parameters
    ----------
    entry : ModelCatalogEntry
        Catalog entry to evaluate; non-Ollama (cloud) entries always return False.
    hardware : HardwareInfo
        Probed hardware. ``vram_gb == 0.0`` (CPU / probe failed) returns False —
        callers handle that case with their own carve-out.
    embed_reserve_gb : float
        Catalog ``vram_gb`` of the active embed model, reserved up front.
    num_ctx : int | None
        Context length the KV cache is costed at; ``None`` uses the catalog default.

    Returns
    -------
    bool
        Whether base residency + beyond-default KV + embed reserve stay within
        the co-residency headroom.
    """
    if hardware.vram_gb <= 0.0 or entry.provider != "ollama":
        return False
    ctx = num_ctx if num_ctx is not None else _resolved_default_num_ctx(entry)
    base_vram = (
        entry.min_vram_gb_at_default_ctx
        if entry.min_vram_gb_at_default_ctx is not None
        else entry.vram_gb
    )
    kv_bytes = (
        entry.kv_cache_bytes_per_token
        if entry.kv_cache_bytes_per_token is not None
        else _DEFAULT_KV_CACHE_BYTES_PER_TOKEN
    )
    # Same marginal-KV model as compute_vram_fit: base residency already
    # includes the default-context KV cache; only tokens beyond it cost extra.
    extra_tokens = max(0, ctx - _resolved_default_num_ctx(entry))
    required_gb = base_vram + extra_tokens * kv_bytes / 1e9 + embed_reserve_gb
    return required_gb <= hardware.vram_gb * _CORESIDENCY_HEADROOM_FRACTION


def safe_num_ctx(
    entry: ModelCatalogEntry,
    hardware: HardwareInfo,
    embed_reserve_gb: float,
) -> int:
    """Largest slider stop that fits in VRAM beside the embed model.

    Walks the frontend slider ladder (``NUM_CTX_LADDER``) up to the catalog
    ``max_num_ctx`` and returns the largest stop whose weights + KV cache +
    embed reserve stay within 85% of VRAM.

    Parameters
    ----------
    entry : ModelCatalogEntry
        Local Ollama catalog entry the context is being chosen for.
    hardware : HardwareInfo
        Probed hardware; ``vram_gb == 0.0`` returns the catalog default.
    embed_reserve_gb : float
        Catalog ``vram_gb`` of the active embed model.

    Returns
    -------
    int
        A ladder stop (or the catalog default on CPU), never above ``max_num_ctx``;
        the smallest stop when even that does not fit.
    """
    resolved_max_ctx = entry.max_num_ctx if entry.max_num_ctx is not None else entry.context_tokens
    if hardware.vram_gb == 0.0:
        return min(_resolved_default_num_ctx(entry), resolved_max_ctx)
    best: int | None = None
    for stop in NUM_CTX_LADDER:
        if stop > resolved_max_ctx:
            break
        if fits_with_embed_reserve(entry, hardware, embed_reserve_gb, num_ctx=stop):
            best = stop
    if best is None:
        best = min(NUM_CTX_LADDER[0], resolved_max_ctx)
    return best


def _status_for_entry(
    entry: ModelCatalogEntry,
    *,
    active: bool,
    fit: Fit,
    provider_key_present: bool,
    installed_names: dict[str, dict[str, Any]],
) -> tuple[Status, bool, dict[str, Any]]:
    """Resolve (status, pulled, installed-payload) for one catalog entry."""
    if entry.provider != "ollama":
        status: Status = "cloud_active" if active and provider_key_present else "cloud_required"
        return status, False, {}
    tag = normalize_model_tag(entry.ollama_tag or entry.id)
    installed_payload = installed_names.get(tag)
    pulled = installed_payload is not None
    if active:
        status = "active"
    elif pulled:
        status = "pulled"
    elif fit == "unfit":
        status = "unfit"
    else:
        status = "downloadable"
    return status, pulled, installed_payload or {}


def provider_display_name(provider_id: str) -> str:
    """Return the operator-facing provider name, falling back to the raw id.

    Blocker copy is user-facing, so it must never show a registry id like
    ``custom_openai_compatible``. Ollama and any drifted id have no registry
    entry; those keep the raw value rather than failing the status surface.
    """
    try:
        return provider_for_id(provider_id).display_name
    except ValueError:
        return provider_id


def provider_access_blocker(provider_id: str) -> str:
    """Return the message telling an operator how to make *provider_id* reachable.

    The picker and the assignment save path both refuse on the same condition, so
    they say the same sentence: two wordings drift, and the operator who reads one
    then hits the other has no way to tell which is authoritative. A provider that
    can run on an endpoint URL alone is not necessarily waiting on a key.
    """
    try:
        needs_url = provider_for_id(provider_id).base_url_config_key is not None
    except ValueError:
        needs_url = False
    needed = "API key or endpoint URL" if needs_url else "API key"
    return (
        f"Configure the {provider_display_name(provider_id)} {needed} before assigning this model."
    )


def _assignability_for_entry(
    entry: ModelCatalogEntry,
    *,
    status: Status,
    pulled: bool,
    active: bool,
    provider_key_present: bool,
) -> tuple[bool, str | None]:
    """Resolve (can_assign, assign_blocker) for one catalog entry."""
    if not entry.assignable:
        return False, (
            entry.notes or "This model is tracked for evaluation but is not assignable yet."
        )
    if entry.provider == "ollama":
        can_assign = pulled or active
        if can_assign:
            return True, None
        if status == "unfit":
            return False, "Model does not fit this machine."
        return False, "Pull this model first."
    can_assign = provider_key_present or active
    return can_assign, None if can_assign else provider_access_blocker(entry.provider)


def _effective_num_ctx_for_entry(entry: ModelCatalogEntry, ctx_per_role: dict[str, int]) -> int:
    """Most permissive user-specified num_ctx across entry's roles, else the catalog default."""
    role_ctxs = [ctx_per_role[r] for r in entry.roles if r in ctx_per_role]
    if role_ctxs:
        return max(role_ctxs)
    return (
        entry.default_num_ctx
        if entry.default_num_ctx is not None
        else min(_DEFAULT_NUM_CTX_FALLBACK, entry.context_tokens)
    )


def build_model_statuses(
    *,
    installed: list[dict[str, Any]],
    current: dict[str, Any],
    embedding_model_name: str,
    hardware: HardwareInfo | None = None,
    cloud_api_keys: dict[str, bool] | None = None,
    num_ctx_per_role: dict[str, int] | None = None,
    extra_entries: Sequence[ModelCatalogEntry] = (),
) -> list[ModelStatusDict]:
    """Combine catalog, installed Ollama models, active assignments, and hardware.

    Parameters
    ----------
    num_ctx_per_role:
        Per-role num_ctx overrides keyed by role name (``"smart"``, ``"fast"``,
        ``"embed"``).  When provided, ``fit_detail`` is computed at the
        user's chosen context length; absent roles fall back to the catalog
        default.  Pass ``None`` (default) to always use catalog defaults.
    extra_entries:
        Entries synthesized outside the bundled catalog (a provider's live
        model list).  They run through exactly the same status, fit and
        assignability machinery, so every runtime key is computed by the code
        that owns it.
    """
    hw = hardware or detect_hardware()
    cloud_keys = cloud_api_keys or {}
    ctx_per_role = num_ctx_per_role or {}
    installed_names = _installed_by_name(installed)
    active_ids = _active_model_ids(current, embedding_model_name)

    statuses: list[ModelStatusDict] = []
    for entry in (*MODEL_CATALOG, *extra_entries):
        payload = entry.to_dict()
        active = normalize_model_tag(entry.id) in active_ids or (
            entry.ollama_tag is not None and normalize_model_tag(entry.ollama_tag) in active_ids
        )
        provider_key_present = bool(cloud_keys.get(entry.provider, False))
        fit = _fit_for_entry(entry, hardware=hw, cloud_key_present=provider_key_present)

        status, pulled, installed_payload = _status_for_entry(
            entry,
            active=active,
            fit=fit,
            provider_key_present=provider_key_present,
            installed_names=installed_names,
        )
        payload.update(installed_payload)

        can_assign, assign_blocker = _assignability_for_entry(
            entry,
            status=status,
            pulled=pulled,
            active=active,
            provider_key_present=provider_key_present,
        )

        effective_num_ctx = _effective_num_ctx_for_entry(entry, ctx_per_role)
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
    extra_entries: Sequence[ModelCatalogEntry] = (),
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
    extra_entries : Sequence[ModelCatalogEntry]
        Live provider entries merged alongside the bundled catalog, so the
        catalog and the recommendations for a role cannot disagree.

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
            extra_entries=extra_entries,
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


async def _stream_pull_progress(resp: httpx.Response, ctx: Any, last_message: str) -> str:
    """Forward Ollama pull progress events to the job stream; returns the latest status."""
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
        if isinstance(total, int | float) and total > 0 and isinstance(completed, int | float):
            progress = min(0.99, max(0.01, float(completed) / float(total)))
            await ctx.update_progress(progress, status)
        elif event.get("status"):
            await ctx.update_progress(0.05, status)
        if event.get("error"):
            raise RuntimeError(str(event["error"]))
    return last_message


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
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    ollama_url = str(
        payload.get("ollama_url") or get_paper_ingestion_settings().ollama_base_url
    ).rstrip("/")
    last_message = "Pulling model"
    ensure_outbound_egress_allowed("Ollama model pull")
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
            last_message = await _stream_pull_progress(resp, ctx, last_message)
    except httpx.HTTPError as exc:
        await ctx.update_progress(0.0, f"Failed: Could not reach Ollama pull API: {exc}")
        raise RuntimeError(f"Could not reach Ollama pull API: {exc}") from exc
    except RuntimeError as exc:
        await ctx.update_progress(0.0, f"Failed: {exc}")
        raise

    await _raise_if_cancelled(ctx)
    await ctx.update_progress(1.0, "Done")
    return {"tag": entry.ollama_tag or entry.id, "status": "pulled", "message": last_message}
