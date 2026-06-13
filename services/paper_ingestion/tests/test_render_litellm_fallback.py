"""Regression tests for scripts/render-litellm-config.sh (de-seed guard).

The switchable model aliases (``smart`` / ``fast`` / ``smart-fallback``) live
in LiteLLM's admin database, delivered via ``POST /model/new`` by the
paper_ingestion boot reconciler and the Settings model picker. They must NOT
exist in ``litellm/config.yaml``: YAML-seeded deployments cannot be removed at
runtime (``/model/delete`` only deletes DB rows), so a YAML ``smart`` would
STACK with its DB replacement and latency-based routing could keep preferring
the stale model.

The render script is therefore a scrub-only guard (no env inputs):

1. removes any smart/fast/smart-fallback entries from ``model_list``
   (upgrade path — older installs seeded them from env vars),
2. keeps the dimension-locked ``embed`` entries untouched,
3. normalizes ``router_settings.fallbacks`` to ``smart → ["smart-fallback"]``
   (the DB-created deployment group), dropping legacy raw provider-model
   fallback strings,
4. is idempotent — a second run changes nothing.

The script derives paths from its own location via BASH_SOURCE, so we copy the
required files into a temp dir preserving the scripts/litellm/ layout, then run
the copied script there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="render-litellm-config.sh needs bash"
)

SWITCHABLE_ALIASES = {"smart", "fast", "smart-fallback"}

# A pre-de-seed config shape (what older installs / the retired env-driven
# renderer produced) — the upgrade path the scrub must handle.
LEGACY_SEEDED_CONFIG: dict[str, Any] = {
    "model_list": [
        {
            "model_name": "smart",
            "litellm_params": {
                "model": "ollama/qwen3:8b",
                "api_base": "http://ollama:11434",
                "temperature": 0.2,
                "num_ctx": 8192,
                "extra_body": {"think": False},
                "timeout": 300,
                "num_retries": 2,
            },
        },
        {
            "model_name": "fast",
            "litellm_params": {
                "model": "ollama/qwen3:4b",
                "api_base": "http://ollama:11434",
                "temperature": 0.1,
                "num_ctx": 4096,
                "extra_body": {"think": False},
            },
        },
        {
            "model_name": "smart-fallback",
            "litellm_params": {
                "model": "ollama/qwen3:4b",
                "api_base": "http://ollama:11434",
                "timeout": 120,
            },
        },
        {
            "model_name": "embed",
            "litellm_params": {
                "model": "ollama/qwen3-embedding:4b",
                "api_base": "http://ollama:11434",
            },
        },
    ],
    "router_settings": {
        "fallbacks": [
            {"smart": ["ollama/qwen3:4b"]},
            {"fast": ["ollama/qwen3:4b"]},
        ]
    },
}


def _setup_tree(tmp_path: Path, config_text: str | None = None) -> Path:
    """Copy the script (+ a config) into tmp_path preserving the repo layout."""
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "litellm").mkdir(exist_ok=True)
    shutil.copy(
        REPO_ROOT / "scripts" / "render-litellm-config.sh",
        tmp_path / "scripts" / "render-litellm-config.sh",
    )
    config_path = tmp_path / "litellm" / "config.yaml"
    if config_text is not None:
        config_path.write_text(config_text)
    else:
        shutil.copy(REPO_ROOT / "litellm" / "config.yaml", config_path)
    return config_path


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the copied script. No env inputs — the script takes none."""
    env = {
        "PATH": subprocess.run(
            ["bash", "-c", "echo $PATH"], capture_output=True, text=True
        ).stdout.strip(),
        "HOME": str(Path.home()),
    }
    return subprocess.run(
        ["bash", str(tmp_path / "scripts" / "render-litellm-config.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_live_repo_config_is_already_deseeded(tmp_path: Path) -> None:
    """The shipped litellm/config.yaml carries no switchable aliases; script no-ops."""
    config_path = _setup_tree(tmp_path)
    before = config_path.read_text()

    live = yaml.safe_load(before)
    seeded = {e["model_name"] for e in live["model_list"]}
    assert not (seeded & SWITCHABLE_ALIASES), (
        f"litellm/config.yaml must not seed switchable aliases; found {seeded}"
    )
    # The dimension-locked embed entries stay YAML-seeded.
    assert "embed" in seeded
    assert live["router_settings"]["fallbacks"] == [{"smart": ["smart-fallback"]}]

    result = _run(tmp_path)

    assert "nothing to do" in result.stdout
    assert config_path.read_text() == before, "no-op run must not rewrite the file"


def test_legacy_seeded_config_is_scrubbed(tmp_path: Path) -> None:
    """Upgrade path: env-seeded smart/fast/smart-fallback entries are removed."""
    config_path = _setup_tree(tmp_path, yaml.safe_dump(LEGACY_SEEDED_CONFIG, sort_keys=False))

    _run(tmp_path)

    config = yaml.safe_load(config_path.read_text())
    names = [e["model_name"] for e in config["model_list"]]
    assert names == ["embed"], f"only the embed entries may survive the scrub; got {names}"
    # The embed entry itself is untouched.
    embed = config["model_list"][0]["litellm_params"]
    assert embed["model"] == "ollama/qwen3-embedding:4b"
    assert embed["api_base"] == "http://ollama:11434"


def test_legacy_fallbacks_normalized_to_deployment_group(tmp_path: Path) -> None:
    """Raw provider-model fallback strings are replaced by the smart-fallback group."""
    config_path = _setup_tree(tmp_path, yaml.safe_dump(LEGACY_SEEDED_CONFIG, sort_keys=False))

    _run(tmp_path)

    config = yaml.safe_load(config_path.read_text())
    assert config["router_settings"]["fallbacks"] == [{"smart": ["smart-fallback"]}], (
        "fallbacks must map smart → ['smart-fallback'] (a real DB deployment group), "
        f"got {config['router_settings']['fallbacks']!r}"
    )


def test_scrub_is_idempotent(tmp_path: Path) -> None:
    """A second run after a scrub reports nothing to do and changes nothing."""
    config_path = _setup_tree(tmp_path, yaml.safe_dump(LEGACY_SEEDED_CONFIG, sort_keys=False))

    _run(tmp_path)
    after_first = config_path.read_text()

    result = _run(tmp_path)
    assert "nothing to do" in result.stdout
    assert config_path.read_text() == after_first


def test_scrub_preserves_live_header_comment(tmp_path: Path) -> None:
    """Scrubbing a stale alias from the REAL config keeps its leading comment block.

    Simulates an upgrade where an older renderer re-seeded a smart entry into
    the shipped config: the scrub must remove the entry while preserving the
    header documentation (where-each-alias-lives rationale).
    """
    live_text = (REPO_ROOT / "litellm" / "config.yaml").read_text()
    # Inject a legacy smart entry right under model_list: (text-level, keeps comments).
    stale_entry = (
        "model_list:\n"
        "  - model_name: smart\n"
        "    litellm_params:\n"
        "      model: ollama/qwen3:8b\n"
        "      api_base: http://ollama:11434\n"
    )
    assert "model_list:" in live_text
    config_path = _setup_tree(tmp_path, live_text.replace("model_list:", stale_entry, 1))

    _run(tmp_path)

    after = config_path.read_text()
    config = yaml.safe_load(after)
    names = {e["model_name"] for e in config["model_list"]}
    assert "smart" not in names
    assert "embed" in names
    # Header comment block survives the rewrite.
    assert "LiteLLM Proxy Configuration" in after
