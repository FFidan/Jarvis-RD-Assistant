"""Regression test for render-litellm-config.sh fallback model resolution.

Ensures the rendered `smart-fallback` alias always resolves to a model that is
in OLLAMA_MODELS (always pulled), not qwen3:1.7b which was never in the pull
set and caused every smart fallback to 404.

The script derives paths from its own location via BASH_SOURCE, so we copy the
three required files into a temp dir preserving the scripts/litellm/config/
layout, then run the copied script there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="render-litellm-config.sh needs bash"
)


def _render(tmp_path: Path, *, tier: str = "24-48", smart_model: str = "qwen3:8b") -> dict:
    """Copy the three required files into tmp_path, run the render script, return parsed YAML."""
    # Preserve scripts/litellm/config/ directory structure
    (tmp_path / "scripts").mkdir()
    (tmp_path / "litellm").mkdir()
    (tmp_path / "config").mkdir()

    shutil.copy(
        REPO_ROOT / "scripts" / "render-litellm-config.sh",
        tmp_path / "scripts" / "render-litellm-config.sh",
    )
    shutil.copy(REPO_ROOT / "litellm" / "config.yaml", tmp_path / "litellm" / "config.yaml")
    shutil.copy(
        REPO_ROOT / "config" / "llm-tier-candidates.yaml",
        tmp_path / "config" / "llm-tier-candidates.yaml",
    )

    env = {
        "JARVIS_LLM_BACKEND": "ollama",
        "JARVIS_SMART_MODEL": smart_model,
        "JARVIS_HW_TIER": tier,
        # Explicitly unset so the script resolves from the yaml
        "PATH": subprocess.run(
            ["bash", "-c", "echo $PATH"], capture_output=True, text=True
        ).stdout.strip(),
        "HOME": str(Path.home()),
    }

    subprocess.run(
        ["bash", str(tmp_path / "scripts" / "render-litellm-config.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    return yaml.safe_load((tmp_path / "litellm" / "config.yaml").read_text())


def test_smart_fallback_is_pulled_model(tmp_path: Path) -> None:
    """smart-fallback must resolve to qwen3:4b (always in OLLAMA_MODELS), not qwen3:1.7b."""
    config = _render(tmp_path, tier="24-48", smart_model="qwen3:8b")

    model_list = config["model_list"]
    fallback_entry = next(e for e in model_list if e["model_name"] == "smart-fallback")

    assert fallback_entry["litellm_params"]["model"] == "ollama/qwen3:4b", (
        "smart-fallback must reference qwen3:4b (always pulled) — "
        f"got {fallback_entry['litellm_params']['model']!r}"
    )


def test_smart_entry_uses_configured_model(tmp_path: Path) -> None:
    """smart alias must reflect the JARVIS_SMART_MODEL env var."""
    config = _render(tmp_path, tier="24-48", smart_model="qwen3:8b")

    model_list = config["model_list"]
    smart_entry = next(e for e in model_list if e["model_name"] == "smart")

    assert smart_entry["litellm_params"]["model"] == "ollama/qwen3:8b", (
        f"smart alias should be ollama/qwen3:8b, got {smart_entry['litellm_params']['model']!r}"
    )


def test_router_fallbacks_maps_smart_to_smart_fallback(tmp_path: Path) -> None:
    """router_settings.fallbacks must map smart → [smart-fallback] after rendering."""
    config = _render(tmp_path, tier="24-48", smart_model="qwen3:8b")

    fallbacks = config["router_settings"]["fallbacks"]
    smart_mapping = next((f for f in fallbacks if "smart" in f), None)

    assert smart_mapping is not None, "No 'smart' key found in router_settings.fallbacks"
    assert smart_mapping["smart"] == ["smart-fallback"], (
        f"Expected smart → ['smart-fallback'], got {smart_mapping['smart']!r}"
    )


@pytest.mark.parametrize("tier", ["cpu", "lt-8", "8-16", "16-24", "24-48", "ge-48"])
def test_all_tiers_resolve_to_pulled_fallback(tmp_path: Path, tier: str) -> None:
    """Every hardware tier's default fallback must be a model in the always-pulled set."""
    always_pulled = {"qwen3:8b", "qwen3:4b", "qwen3-embedding:4b"}
    # Use a fresh tmp_path per tier (parametrize gives one tmp_path per test invocation)
    config = _render(tmp_path, tier=tier, smart_model="qwen3:8b")

    model_list = config["model_list"]
    fallback_entry = next(e for e in model_list if e["model_name"] == "smart-fallback")
    rendered_model = fallback_entry["litellm_params"]["model"]

    # Strip "ollama/" prefix for set membership check
    model_name = rendered_model.removeprefix("ollama/")
    assert model_name in always_pulled, (
        f"Tier {tier!r}: fallback model {model_name!r} is not in OLLAMA_MODELS "
        f"(always-pulled set: {always_pulled})"
    )
