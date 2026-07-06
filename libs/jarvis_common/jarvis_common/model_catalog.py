"""Static model catalog helpers for lifecycle surfaces."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from importlib import resources
from typing import Any, Literal

logger = logging.getLogger(__name__)

Role = Literal["smart", "fast", "embed"]
Provider = Literal[
    "ollama",
    "anthropic",
    "openai",
    "google",
    "openrouter",
    "deepseek",
    "mistral",
    "moonshot",
    "zai",
    "custom_openai_compatible",
]
CatalogPhase = Literal["default", "advanced", "future"]


@dataclass(frozen=True)
class ModelCatalogEntry:
    """Immutable descriptor for a single model in the bundled static catalog."""

    id: str
    name: str
    provider: Provider
    ollama_tag: str | None
    roles: tuple[Role, ...]
    vram_gb: float
    disk_gb: float
    context_tokens: int
    license: str
    tier: int
    description: str
    notes: str
    last_reviewed: str
    embedding_dimension: int | None = None
    phase: CatalogPhase = "default"
    assignable: bool = True
    min_vram_gb_at_default_ctx: float | None = None
    kv_cache_bytes_per_token: int | None = None
    default_num_ctx: int | None = None
    max_num_ctx: int | None = None
    supports_thinking: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize this entry to a plain dict suitable for JSON responses."""
        return asdict(self)


def _coerce_entry(raw: dict) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id=str(raw["id"]),
        name=str(raw["name"]),
        provider=raw["provider"],
        ollama_tag=raw.get("ollama_tag"),
        roles=tuple(raw["roles"]),
        vram_gb=float(raw["vram_gb"]),
        disk_gb=float(raw["disk_gb"]),
        context_tokens=int(raw["context_tokens"]),
        license=str(raw["license"]),
        tier=int(raw["tier"]),
        description=str(raw["description"]),
        notes=str(raw.get("notes", "")),
        last_reviewed=str(raw["last_reviewed"]),
        embedding_dimension=(
            int(raw["embedding_dimension"]) if raw.get("embedding_dimension") is not None else None
        ),
        phase=raw.get("phase", "default"),
        assignable=bool(raw.get("assignable", True)),
        min_vram_gb_at_default_ctx=(
            float(raw["min_vram_gb_at_default_ctx"])
            if raw.get("min_vram_gb_at_default_ctx") is not None
            else None
        ),
        kv_cache_bytes_per_token=(
            int(raw["kv_cache_bytes_per_token"])
            if raw.get("kv_cache_bytes_per_token") is not None
            else None
        ),
        default_num_ctx=(
            int(raw["default_num_ctx"]) if raw.get("default_num_ctx") is not None else None
        ),
        max_num_ctx=(int(raw["max_num_ctx"]) if raw.get("max_num_ctx") is not None else None),
        supports_thinking=bool(raw.get("supports_thinking", False)),
    )


def load_model_catalog() -> tuple[ModelCatalogEntry, ...]:
    """Load the bundled static model catalog.

    The catalog is package data, not a live registry. Runtime code should never
    fetch Ollama or cloud provider registries to mutate this list.
    """
    data = resources.files("jarvis_common.data").joinpath("model_catalog.json").read_text()
    raw_entries = json.loads(data)
    entries = tuple(_coerce_entry(item) for item in raw_entries)
    ids = [entry.id for entry in entries]
    if len(ids) != len(set(ids)):
        raise RuntimeError("model catalog contains duplicate ids")
    return entries


_STALE_THRESHOLD_DAYS = 90


def warn_if_catalog_stale(
    entries: tuple[ModelCatalogEntry, ...],
    *,
    today: date | None = None,
) -> None:
    """Log a warning for catalog entries older than _STALE_THRESHOLD_DAYS days."""
    now = today or date.today()
    for entry in entries:
        try:
            reviewed = date.fromisoformat(entry.last_reviewed)
        except ValueError:
            logger.warning(
                "model catalog entry %s has invalid last_reviewed=%r",
                entry.id,
                entry.last_reviewed,
            )
            continue
        elapsed = (now - reviewed).days
        if elapsed > _STALE_THRESHOLD_DAYS:
            logger.warning(
                "model catalog entry %s was last reviewed on %s (%d days ago)",
                entry.id,
                entry.last_reviewed,
                elapsed,
            )
