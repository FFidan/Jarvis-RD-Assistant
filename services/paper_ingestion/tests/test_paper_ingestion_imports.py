"""Direct tests for the script-side paper_ingestion import bridge."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import _paper_ingestion_imports as imports_mod  # noqa: E402


def test_llm_client_path_points_to_shared_helper():
    """The bridge should resolve to the shared paper_ingestion LiteLLM helper."""
    path = imports_mod._llm_client_path()

    assert path == (
        _PROJECT_ROOT / "services" / "paper_ingestion" / "app" / "llm_client.py"
    )
    assert path.name == "llm_client.py"
    assert path.is_file()


def test_load_llm_client_module_is_cached_and_exports_helpers():
    """Repeated loads should reuse the cached module and expose the shared helpers."""
    module_one = imports_mod._load_llm_client_module()
    module_two = imports_mod._load_llm_client_module()

    assert module_one is module_two
    assert imports_mod.LiteLLMConfig is imports_mod._llm_client.LiteLLMConfig
    assert imports_mod.embed_texts is imports_mod._llm_client.embed_texts
    assert imports_mod.get_litellm_config is imports_mod._llm_client.get_litellm_config


def test_load_llm_client_module_raises_when_loader_is_missing(monkeypatch):
    """A missing import spec loader should raise a clear ImportError."""
    imports_mod._load_llm_client_module.cache_clear()
    monkeypatch.setattr(
        imports_mod.importlib.util,
        "spec_from_file_location",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(ImportError, match="Could not load LiteLLM helpers"):
        imports_mod._load_llm_client_module()

    imports_mod._load_llm_client_module.cache_clear()
