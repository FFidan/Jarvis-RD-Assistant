"""Static model catalog helpers for lifecycle surfaces."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from importlib import resources
from typing import Any, Literal, NotRequired, TypedDict

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
MetadataField = Literal[
    "context_tokens",
    "description",
    "input_price_per_million",
    "output_price_per_million",
    "capabilities",
    "lifecycle",
]


class MetadataFieldSource(TypedDict):
    """Origin for a sparse model metadata field exposed to administrators."""

    kind: Literal["api_reported", "reviewed_catalog"]
    fetched_at: NotRequired[str]
    source_url: NotRequired[str]
    reviewed_at: NotRequired[str]


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
    input_price_per_million: str | None = None
    output_price_per_million: str | None = None
    price_source: str | None = None
    capabilities: tuple[str, ...] = ()
    lifecycle: str | None = None
    field_sources: dict[MetadataField, MetadataFieldSource] = field(default_factory=dict)

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
        input_price_per_million=raw.get("input_price_per_million"),
        output_price_per_million=raw.get("output_price_per_million"),
        price_source=raw.get("price_source"),
        capabilities=tuple(raw.get("capabilities", ())),
        lifecycle=raw.get("lifecycle"),
        field_sources=raw.get("field_sources", {}),
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
