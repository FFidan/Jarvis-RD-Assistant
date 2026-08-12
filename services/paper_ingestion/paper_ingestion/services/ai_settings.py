"""Catalog-backed helpers for the AI settings control plane."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jarvis_common.model_catalog import ModelCatalogEntry, load_model_catalog

from paper_ingestion.services.model_prefixes import strip_latest_tag, strip_ollama_prefix

_CONFIG_FILE = Path("config/llm-tier-candidates.yaml")
_SUPPORTED_BACKENDS = {"ollama", "vllm"}
_SAFE_VLLM_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_TIER_TO_CATALOG_TIER = {
    "cpu": 0,
    "lt-8": 1,
    "8-16": 2,
    "16-24": 3,
    "24-48": 3,
    "ge-48": 4,
}


@dataclass(frozen=True)
class CandidateSelection:
    """Resolved candidates for one hardware tier."""

    candidates: list[dict[str, Any]]
    issues: list[str]
    generated_from: str | None
    generated_at: str | None

    @property
    def recommended(self) -> dict[str, Any]:
        return self.candidates[0]


def find_candidate_config_path() -> Path:
    """Find config/llm-tier-candidates.yaml from source or container layouts."""
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / _CONFIG_FILE
        if candidate.exists():
            return candidate
    return Path("/app") / _CONFIG_FILE


def _normalize_model_id(model_id: str) -> str:
    return strip_latest_tag(strip_ollama_prefix(model_id.strip()))


def _catalog_lookup(
    catalog_entries: Sequence[ModelCatalogEntry],
) -> dict[str, ModelCatalogEntry]:
    lookup: dict[str, ModelCatalogEntry] = {}
    for entry in catalog_entries:
        lookup[_normalize_model_id(entry.id)] = entry
        if entry.ollama_tag:
            lookup[_normalize_model_id(entry.ollama_tag)] = entry
    return lookup


def _load_candidate_data(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {"tiers": {}}
    with config_path.open() as f:
        return yaml.safe_load(f) or {"tiers": {}}


def _issue(tier: str, raw: Mapping[str, Any], message: str) -> str:
    backend = raw.get("backend", "<missing>")
    model = raw.get("model", "<missing>")
    rank = raw.get("rank", "?")
    return f"{tier} rank {rank} {backend}/{model}: {message}"


def _has_rank(raw: Mapping[str, Any]) -> bool:
    return raw.get("rank") is not None


def _is_safe_vllm_model_id(model: str) -> bool:
    return bool(_SAFE_VLLM_MODEL_RE.fullmatch(model))


def _candidate_from_catalog_entry(
    entry: ModelCatalogEntry,
    *,
    rank: int,
    reasoning: str | None = None,
) -> dict[str, Any]:
    return {
        "backend": "ollama",
        "model": entry.id,
        "catalog_id": entry.id,
        "source": "catalog",
        "rank": rank,
        "score": None,
        "evidence": "catalog",
        "reasoning": reasoning or entry.description,
    }


def catalog_recommendation_for_tier(
    tier: str,
    *,
    catalog_entries: Sequence[ModelCatalogEntry] | None = None,
) -> dict[str, Any]:
    """Return the curated-catalog fallback candidate for a hardware tier."""
    catalog = tuple(catalog_entries or load_model_catalog())
    max_tier = _TIER_TO_CATALOG_TIER.get(tier, 0)
    smart_local = [
        entry
        for entry in catalog
        if entry.provider == "ollama" and "smart" in entry.roles and entry.assignable
    ]
    fitting = [entry for entry in smart_local if entry.tier <= max_tier]
    pool = fitting or smart_local
    if not pool:
        raise RuntimeError("curated model catalog has no assignable local smart models")
    best = min(pool, key=lambda entry: (entry.tier > max_tier, entry.tier, entry.vram_gb, entry.id))
    return _candidate_from_catalog_entry(best, rank=1)


def resolve_candidates_for_tier(
    tier: str,
    *,
    config_path: Path | None = None,
    catalog_entries: Sequence[ModelCatalogEntry] | None = None,
) -> CandidateSelection:
    """Resolve the empirical overlay under the curated catalog contract."""
    path = config_path or find_candidate_config_path()
    data = _load_candidate_data(path)
    catalog = tuple(catalog_entries or load_model_catalog())
    lookup = _catalog_lookup(catalog)
    tier_entry = data.get("tiers", {}).get(tier, {}) or {}
    raw_candidates = tier_entry.get("candidates", []) or []
    issues: list[str] = []
    candidates: list[dict[str, Any]] = []

    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            issues.append(f"{tier} candidate is not an object: {raw!r}")
            continue
        backend = str(raw.get("backend", "")).strip()
        model = str(raw.get("model", "")).strip()
        if backend not in _SUPPORTED_BACKENDS:
            issues.append(_issue(tier, raw, "backend is not supported by this settings surface"))
            continue
        if not _has_rank(raw):
            issues.append(_issue(tier, raw, "candidate is missing rank"))
            continue

        if backend == "vllm":
            if not _is_safe_vllm_model_id(model):
                issues.append(_issue(tier, raw, "vLLM model id is empty or unsafe"))
                continue
            payload = dict(raw)
            payload["backend"] = "vllm"
            payload["model"] = model
            payload["catalog_id"] = None
            payload["source"] = "tier-candidates"
            candidates.append(payload)
            continue

        entry = lookup.get(_normalize_model_id(model))
        if entry is None:
            issues.append(_issue(tier, raw, "model is not in the curated model catalog"))
            continue
        if entry.provider != "ollama":
            issues.append(
                _issue(
                    tier,
                    raw,
                    "candidate is not an assignable local Ollama catalog entry for this endpoint",
                )
            )
            continue
        if not entry.assignable:
            issues.append(_issue(tier, raw, "catalog entry is not assignable"))
            continue
        if "smart" not in entry.roles:
            issues.append(_issue(tier, raw, "catalog entry does not support the smart role"))
            continue

        payload = dict(raw)
        payload["backend"] = "ollama"
        payload["model"] = entry.id
        payload["catalog_id"] = entry.id
        payload["source"] = "catalog"
        candidates.append(payload)

    if not candidates:
        fallback = catalog_recommendation_for_tier(tier, catalog_entries=catalog)
        issues.append(
            f"{tier}: no valid empirical candidates remained; "
            f"using curated catalog recommendation {fallback['backend']}/{fallback['model']}"
        )
        candidates.append(fallback)

    # YAML parses an unquoted ISO date (generated_at: 2026-05-22) as a date
    # object; coerce to a string so the response model and UI get a plain date.
    generated_at_raw = data.get("generated_at")
    return CandidateSelection(
        candidates=candidates,
        issues=issues,
        generated_from=data.get("generated_from"),
        generated_at=str(generated_at_raw) if generated_at_raw is not None else None,
    )
