from __future__ import annotations

from jarvis_common.model_catalog import load_model_catalog


def test_model_catalog_contains_contract_entries() -> None:
    catalog = load_model_catalog()
    ids = {entry.id for entry in catalog}

    assert len(catalog) == 13
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


def test_advanced_embedding_candidates_are_not_assignable_by_default() -> None:
    catalog = {entry.id: entry for entry in load_model_catalog()}

    assert catalog["qwen3-embedding:0.6b"].assignable is True
    assert catalog["qwen3-embedding:0.6b"].embedding_dimension == 1024
    assert catalog["qwen3-embedding:4b"].phase == "advanced"
    assert catalog["qwen3-embedding:4b"].assignable is False
    assert catalog["openai/text-embedding-3-small"].assignable is False
