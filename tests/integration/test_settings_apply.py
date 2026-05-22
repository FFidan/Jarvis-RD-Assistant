"""Boot the stack, switch backend via API, verify next /api/ask uses new model.

Gated by SMOKE_INTEGRATION=1 — never runs in normal CI.

To execute manually:
    SMOKE_INTEGRATION=1 uv run pytest tests/integration/test_settings_apply.py -v

Prerequisites:
    - Full stack running (docker compose up -d)
    - secrets/jarvis_api_key.txt populated
    - The target model (qwen3:1.7b) pulled and available in Ollama/vLLM
"""

import os

import httpx
import pytest


@pytest.mark.skipif(os.getenv("SMOKE_INTEGRATION") != "1", reason="integration gated")
def test_settings_apply_switches_model():
    base = os.getenv("PAPER_INGESTION_BASE", "http://localhost:8000")
    api_key = open("secrets/jarvis_api_key.txt").read().strip()
    headers = {"X-API-Key": api_key}

    # Switch to a small model
    resp = httpx.post(
        f"{base}/api/settings/ai",
        headers=headers,
        json={"backend": "ollama", "model": "qwen3:1.7b"},
        timeout=120,
    )
    assert resp.status_code == 200, resp.text

    # Issue an /api/ask; capture x-litellm-model-id header
    resp = httpx.post(
        f"{base}/api/ask",
        headers=headers,
        json={"question": "Echo: hello.", "decompose": False},
        timeout=60,
    )
    assert resp.status_code == 200
    assert "qwen3:1.7b" in resp.headers.get("x-litellm-model-id", "")
