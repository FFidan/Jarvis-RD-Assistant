from __future__ import annotations

from jarvis_common.model_catalog import load_model_catalog


def test_model_catalog_contains_contract_entries() -> None:
    catalog = load_model_catalog()
    ids = {entry.id for entry in catalog}

    assert "qwen3-embedding:0.6b" in ids
    assert "qwen3-embedding:4b" in ids
    assert "anthropic/claude-sonnet-4-6" in ids
    assert "mistral-nemo" not in ids
    assert "nomic-embed-text" not in ids


def test_model_catalog_entries_have_unique_ids_and_review_dates() -> None:
    catalog = load_model_catalog()
    ids = [entry.id for entry in catalog]

    assert len(ids) == len(set(ids))
    assert all(entry.last_reviewed for entry in catalog)


def test_gpt_oss_is_pullable_but_not_a_default() -> None:
    catalog = {entry.id: entry for entry in load_model_catalog()}
    entry = catalog["gpt-oss:20b"]

    assert entry.provider == "ollama"
    assert entry.ollama_tag == "gpt-oss:20b"
    assert entry.phase == "future"
    assert entry.assignable is False
    assert entry.supports_thinking is True
    assert entry.min_vram_gb_at_default_ctx == 16.0


def test_embedding_candidates_have_expected_assignability_defaults() -> None:
    catalog = {entry.id: entry for entry in load_model_catalog()}

    assert catalog["qwen3-embedding:0.6b"].assignable is True
    assert catalog["qwen3-embedding:0.6b"].embedding_dimension == 1024
    assert catalog["qwen3-embedding:4b"].phase == "default"
    assert catalog["qwen3-embedding:4b"].assignable is True
    assert catalog["qwen3-embedding:4b"].embedding_dimension == 2560
    assert catalog["openai/text-embedding-3-small"].assignable is False


def test_bundled_catalog_entries_default_to_unknown_provider_pricing() -> None:
    """Static and local catalog entries do not invent provider pricing."""
    entry = load_model_catalog()[0]

    assert entry.input_price_per_million is None
    assert entry.output_price_per_million is None
    assert entry.price_source is None


def test_catalog_entries_expose_sparse_typed_field_provenance() -> None:
    entry = next(item for item in load_model_catalog() if item.id == "anthropic/claude-sonnet-4-6")

    assert entry.field_sources["description"]["kind"] == "reviewed_catalog"
    assert entry.field_sources["description"]["source_url"].startswith("https://")
