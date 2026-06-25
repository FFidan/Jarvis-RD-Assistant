"""Pure-function tests for the Ollama provider-prefix helpers."""

from __future__ import annotations

import pytest

from paper_ingestion.services.model_prefixes import (
    OLLAMA_PREFIXES,
    is_local_ollama,
    strip_ollama_prefix,
)


def test_prefixes_cover_both_transports():
    assert OLLAMA_PREFIXES == ("ollama/", "ollama_chat/")


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("ollama/qwen3:8b", "qwen3:8b"),
        ("ollama_chat/qwen3:8b", "qwen3:8b"),
        ("ollama_chat/qwen3:4b", "qwen3:4b"),
        ("anthropic/claude-sonnet", "anthropic/claude-sonnet"),
        ("qwen3:8b", "qwen3:8b"),
        ("", ""),
    ],
)
def test_strip_ollama_prefix(model, expected):
    assert strip_ollama_prefix(model) == expected


def test_strip_longest_match_first():
    # ``ollama_chat/`` must win over the shorter ``ollama/`` so the bare tag is
    # not left with a stray ``_chat/`` fragment.
    assert strip_ollama_prefix("ollama_chat/llama3") == "llama3"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("ollama/qwen3:8b", True),
        ("ollama_chat/qwen3:8b", True),
        ("anthropic/claude-sonnet", False),
        ("qwen3:8b", False),
    ],
)
def test_is_local_ollama(model, expected):
    assert is_local_ollama(model) is expected
