"""Catalog-backed helpers for the AI settings control plane."""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jarvis_common.model_catalog import ModelCatalogEntry, load_model_catalog

from paper_ingestion.services.model_prefixes import strip_ollama_prefix

_CONFIG_FILE = Path("config/llm-tier-candidates.yaml")
_SUPPORTED_BACKENDS = {"ollama", "vllm"}
_SAFE_VLLM_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_APPLY_ENV_KEYS = ("JARVIS_LLM_BACKEND", "JARVIS_SMART_MODEL", "COMPOSE_PROFILES")
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

    @property
    def recommended(self) -> dict[str, Any]:
        return self.candidates[0]


@dataclass(frozen=True)
class EnvSnapshot:
    environ: dict[str, str | None]
    env_file: dict[str, str | None]
    file_existed: bool


def find_candidate_config_path() -> Path:
    """Find config/llm-tier-candidates.yaml from source or container layouts."""
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / _CONFIG_FILE
        if candidate.exists():
            return candidate
    return Path("/app") / _CONFIG_FILE


def _normalize_model_id(model_id: str) -> str:
    value = strip_ollama_prefix(model_id.strip())
    return value.removesuffix(":latest")


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

    return CandidateSelection(
        candidates=candidates,
        issues=issues,
        generated_from=data.get("generated_from"),
    )


def _models_match_for_backend(*, backend: str, candidate_model: str, requested_model: str) -> bool:
    if backend == "ollama":
        return _normalize_model_id(candidate_model) == _normalize_model_id(requested_model)
    return candidate_model.strip() == requested_model.strip()


def candidate_is_allowed(selection: CandidateSelection, *, backend: str, model: str) -> bool:
    return any(
        candidate["backend"] == backend
        and _models_match_for_backend(
            backend=backend,
            candidate_model=str(candidate["model"]),
            requested_model=model,
        )
        for candidate in selection.candidates
    )


class EnvFileStore:
    """Small .env adapter with exact env-var restore support."""

    def __init__(
        self,
        path: Path | str = ".env",
        *,
        environ: MutableMapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.environ = environ if environ is not None else os.environ

    def snapshot(self, keys: Sequence[str] = _APPLY_ENV_KEYS) -> EnvSnapshot:
        return EnvSnapshot(
            environ={key: self.environ.get(key) for key in keys},
            env_file=self._read_env_file_values(keys),
            file_existed=self.path.exists(),
        )

    def apply(self, updates: Mapping[str, str]) -> None:
        self._write_env_file_values(updates)
        for key, value in updates.items():
            self.environ[key] = value

    def restore(self, snapshot: EnvSnapshot) -> None:
        if snapshot.file_existed:
            self._write_env_file_values(snapshot.env_file)
        for key, value in snapshot.environ.items():
            if value is None:
                self.environ.pop(key, None)
            else:
                self.environ[key] = value

    def _read_env_file_values(self, keys: Sequence[str]) -> dict[str, str | None]:
        values: dict[str, str | None] = {key: None for key in keys}
        if not self.path.exists():
            return values
        for line in self.path.read_text().splitlines():
            for key in keys:
                if line.startswith(f"{key}="):
                    values[key] = line.split("=", 1)[1]
        return values

    def _write_env_file_values(self, values: Mapping[str, str | None]) -> None:
        if not self.path.exists():
            return
        lines = self.path.read_text().splitlines()
        seen: set[str] = set()
        next_lines: list[str] = []
        for line in lines:
            key = next((name for name in values if line.startswith(f"{name}=")), None)
            if key is None:
                next_lines.append(line)
                continue
            seen.add(key)
            value = values[key]
            if value is not None:
                next_lines.append(f"{key}={value}")
        for key, value in values.items():
            if key not in seen and value is not None:
                next_lines.append(f"{key}={value}")
        self.path.write_text("\n".join(next_lines) + "\n")


def _default_health_check() -> bool:
    try:
        urllib.request.urlopen("http://localhost:8000/health/live", timeout=2).read()
    except Exception:
        return False
    return True


class AISettingsApplier:
    """Apply model settings using injectable process and environment boundaries."""

    def __init__(
        self,
        *,
        env_store: EnvFileStore | None = None,
        run_command: Callable[..., Any] = subprocess.run,
        health_check: Callable[[], bool] = _default_health_check,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        health_timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self.env_store = env_store or EnvFileStore()
        self.run_command = run_command
        self.health_check = health_check
        self.now = now
        self.sleep = sleep
        self.health_timeout_s = health_timeout_s
        self.poll_interval_s = poll_interval_s

    def apply(self, *, backend: str, model: str, tier: str) -> None:
        snapshot = self.env_store.snapshot()
        updates = {
            "JARVIS_LLM_BACKEND": backend,
            "JARVIS_SMART_MODEL": model,
            "COMPOSE_PROFILES": "vllm" if backend == "vllm" else "",
        }
        try:
            self.env_store.apply(updates)
            render_env = {
                **self.env_store.environ,
                "JARVIS_LLM_BACKEND": backend,
                "JARVIS_SMART_MODEL": model,
                "JARVIS_HW_TIER": tier,
            }
            self.run_command(
                ["bash", "scripts/render-litellm-config.sh"],
                env=render_env,
                check=True,
                capture_output=True,
            )
            self.run_command(
                ["docker", "compose", "up", "-d"],
                check=True,
                capture_output=True,
            )
            self._wait_until_healthy()
        except Exception:
            self.env_store.restore(snapshot)
            raise

    def _wait_until_healthy(self) -> None:
        deadline = self.now() + self.health_timeout_s
        while self.now() < deadline:
            if self.health_check():
                return
            self.sleep(self.poll_interval_s)
        raise RuntimeError(
            f"backend /health/live did not become ready within {self.health_timeout_s:g}s"
        )
