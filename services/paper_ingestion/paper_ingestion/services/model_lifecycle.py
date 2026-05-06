"""Model catalog, hardware probing, status helpers, and pull job."""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

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
    vram_gb: float
    vram_source: Literal["nvidia-smi", "macos-approx", "cpu"]
    tier: int
    detected_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_model_tag(tag: str) -> str:
    """Normalize implicit latest suffixes and LiteLLM provider prefixes."""
    value = tag.strip()
    if value.startswith("ollama/"):
        value = value.removeprefix("ollama/")
    return value.removesuffix(":latest")


def hardware_tier(vram_gb: float) -> int:
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
        digits = "".join(ch for ch in line if ch.isdigit() or ch == ".")
        if not digits:
            continue
        try:
            mb = float(digits)
        except ValueError:
            continue
        return mb / 1024.0
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
    normalized = normalize_model_tag(model_id)
    for entry in MODEL_CATALOG:
        if normalize_model_tag(entry.id) == normalized:
            return entry
        if entry.ollama_tag and normalize_model_tag(entry.ollama_tag) == normalized:
            return entry
    return None


def cloud_provider_for_model(model_id: str) -> str | None:
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


def build_model_statuses(
    *,
    installed: list[dict[str, Any]],
    current: dict[str, Any],
    embedding_model_name: str,
    hardware: HardwareInfo | None = None,
    cloud_api_keys: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Combine catalog, installed Ollama models, active assignments, and hardware."""
    hw = hardware or detect_hardware()
    cloud_keys = cloud_api_keys or {}
    installed_names = _installed_by_name(installed)
    active_ids = _active_model_ids(current, embedding_model_name)

    statuses: list[dict[str, Any]] = []
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
) -> list[dict[str, Any]]:
    """Return catalog entries for one role, ranked by fit and readiness."""
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
