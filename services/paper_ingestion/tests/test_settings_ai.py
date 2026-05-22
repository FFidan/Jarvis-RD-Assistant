"""Tests for the admin AI settings control plane."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.auth import verify_api_key
from jarvis_common.testing import RoleMiddleware

from paper_ingestion.deps import get_db_pool
from paper_ingestion.main import app
from paper_ingestion.routers import settings_ai
from paper_ingestion.services.ai_settings import (
    AISettingsApplier,
    EnvFileStore,
    resolve_candidates_for_tier,
)


@pytest.fixture()
def _base_app(mock_db):
    """Return (app, pool, conn) with rate-limiter + verify_api_key bypassed."""
    pool, conn = mock_db
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, pool, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.fixture()
def _ai_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "llm-tier-candidates.yaml"
    path.write_text(
        """
generated_from: bench.md
tiers:
  ge-48:
    candidates:
      - backend: ollama
        model: qwen3:14b
        rank: 1
        score: 92
        evidence: bench
        reasoning: catalog-backed candidate
""".strip()
        + "\n"
    )
    monkeypatch.setattr(settings_ai, "_CONFIG_PATH", path)
    return path


class _FakeApplier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def apply(self, *, backend: str, model: str, tier: str) -> None:
        self.calls.append({"backend": backend, "model": model, "tier": tier})
        if self.fail:
            raise RuntimeError("compose failed")


def _admin_client(app) -> httpx.AsyncClient:
    wrapped = RoleMiddleware(app, "admin")
    return httpx.AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test")


def _anon_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_candidate_overlay_preserves_vllm_and_normalizes_ollama_catalog(
    tmp_path: Path,
) -> None:
    config = tmp_path / "candidates.yaml"
    config.write_text(
        """
generated_from: bench.md
tiers:
  ge-48:
    candidates:
      - backend: vllm
        model: Qwen/Qwen3-14B-AWQ
        rank: 1
      - backend: ollama
        model: qwen3:14b
        rank: 2
""".strip()
        + "\n"
    )

    selection = resolve_candidates_for_tier("ge-48", config_path=config)

    assert [candidate["model"] for candidate in selection.candidates] == [
        "Qwen/Qwen3-14B-AWQ",
        "qwen3:14b",
    ]
    assert selection.candidates[0]["backend"] == "vllm"
    assert selection.candidates[0]["catalog_id"] is None
    assert selection.candidates[0]["source"] == "tier-candidates"
    assert selection.candidates[1]["backend"] == "ollama"
    assert selection.candidates[1]["catalog_id"] == "qwen3:14b"
    assert selection.candidates[1]["source"] == "catalog"
    assert selection.generated_from == "bench.md"
    assert selection.issues == []


def test_real_low_tier_config_falls_back_to_small_catalog_model() -> None:
    """Rejected low-tier YAML rows must not escalate to a larger catalog model."""
    from paper_ingestion.services.ai_settings import find_candidate_config_path

    for tier in ("cpu", "lt-8", "8-16"):
        selection = resolve_candidates_for_tier(tier, config_path=find_candidate_config_path())
        assert selection.candidates[0]["backend"] == "ollama"
        assert selection.candidates[0]["model"] == "qwen3:4b"
        assert selection.candidates[0]["catalog_id"] == "qwen3:4b"
        assert selection.candidates[0]["source"] == "catalog"
        assert any("no valid empirical candidates" in issue for issue in selection.issues)


def test_ai_settings_applier_restores_env_and_env_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "JARVIS_LLM_BACKEND=ollama\n"
        "JARVIS_SMART_MODEL=qwen3:8b\n"
        "COMPOSE_PROFILES=old-profile\n"
        "OTHER=value\n"
    )
    monkeypatch.setenv("JARVIS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("JARVIS_SMART_MODEL", "qwen3:8b")
    monkeypatch.setenv("COMPOSE_PROFILES", "old-profile")

    def fail_on_compose(cmd, **_kwargs):
        if cmd[:2] == ["docker", "compose"]:
            raise RuntimeError("compose failed")

    applier = AISettingsApplier(
        env_store=EnvFileStore(env_path),
        run_command=fail_on_compose,
        health_check=lambda: True,
    )

    with pytest.raises(RuntimeError, match="compose failed"):
        applier.apply(backend="ollama", model="qwen3:14b", tier="ge-48")

    assert os.environ["JARVIS_LLM_BACKEND"] == "ollama"
    assert os.environ["JARVIS_SMART_MODEL"] == "qwen3:8b"
    assert os.environ["COMPOSE_PROFILES"] == "old-profile"
    assert env_path.read_text() == (
        "JARVIS_LLM_BACKEND=ollama\n"
        "JARVIS_SMART_MODEL=qwen3:8b\n"
        "COMPOSE_PROFILES=old-profile\n"
        "OTHER=value\n"
    )


@pytest.mark.asyncio
async def test_get_settings_ai_returns_catalog_backed_candidates(
    _base_app,
    _ai_config: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, _pool, _conn = _base_app
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setenv("JARVIS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("JARVIS_SMART_MODEL", "qwen3:14b")
    monkeypatch.setattr(settings_ai, "observed_share", lambda _role: ("ollama/qwen3:14b", 1.0))

    async with _admin_client(app) as client:
        resp = await client.get("/api/settings/ai")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hw_tier"] == "ge-48"
    assert body["recommended_backend"] == "ollama"
    assert body["recommended_model"] == "qwen3:14b"
    assert body["configured_backend"] == "ollama"
    assert body["configured_model"] == "qwen3:14b"
    assert body["observed_backend"] == "ollama/qwen3:14b"
    assert body["candidate_issues"] == []
    assert body["candidates_for_tier"][0]["catalog_id"] == "qwen3:14b"


@pytest.mark.asyncio
async def test_get_settings_ai_requires_admin(_base_app):
    app, _pool, _conn = _base_app

    async with _anon_client(app) as client:
        resp = await client.get("/api/settings/ai")

    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_post_settings_ai_allows_resolved_vllm_candidate(
    _base_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, _pool, _conn = _base_app
    config = tmp_path / "llm-tier-candidates.yaml"
    config.write_text(
        """
tiers:
  ge-48:
    candidates:
      - backend: vllm
        model: Qwen/Qwen3-14B-AWQ
        rank: 1
""".strip()
        + "\n"
    )
    applier = _FakeApplier()
    monkeypatch.setattr(settings_ai, "_CONFIG_PATH", config)
    monkeypatch.setattr(settings_ai, "_APPLIER", applier)
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setattr(settings_ai, "observed_share", lambda _role: (None, 0.0))

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai",
            json={"backend": "vllm", "model": "Qwen/Qwen3-14B-AWQ"},
        )

    assert resp.status_code == 200, resp.text
    assert applier.calls == [{"backend": "vllm", "model": "Qwen/Qwen3-14B-AWQ", "tier": "ge-48"}]
    body = resp.json()
    assert body["recommended_backend"] == "vllm"
    assert body["recommended_model"] == "Qwen/Qwen3-14B-AWQ"
    assert body["candidates_for_tier"][0]["catalog_id"] is None
    assert body["candidates_for_tier"][0]["source"] == "tier-candidates"


@pytest.mark.asyncio
async def test_post_settings_ai_rejects_random_non_candidate_model(
    _base_app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, _pool, _conn = _base_app
    config = tmp_path / "llm-tier-candidates.yaml"
    config.write_text(
        """
tiers:
  ge-48:
    candidates:
      - backend: vllm
        model: Qwen/Qwen3-14B-AWQ
        rank: 1
""".strip()
        + "\n"
    )
    applier = _FakeApplier()
    monkeypatch.setattr(settings_ai, "_CONFIG_PATH", config)
    monkeypatch.setattr(settings_ai, "_APPLIER", applier)
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai",
            json={"backend": "vllm", "model": "Qwen/Not-The-Bench-Candidate"},
        )
        suffixed_resp = await client.post(
            "/api/settings/ai",
            json={"backend": "vllm", "model": "Qwen/Qwen3-14B-AWQ:latest"},
        )

    assert resp.status_code == 422, resp.text
    assert "candidates_for_tier" in resp.json().get("detail", "")
    assert suffixed_resp.status_code == 422, suffixed_resp.text
    assert "candidates_for_tier" in suffixed_resp.json().get("detail", "")
    assert applier.calls == []


@pytest.mark.asyncio
async def test_post_settings_ai_happy_path_uses_injected_applier(
    _base_app,
    _ai_config: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, _pool, _conn = _base_app
    applier = _FakeApplier()
    monkeypatch.setattr(settings_ai, "_APPLIER", applier)
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setattr(settings_ai, "observed_share", lambda _role: ("ollama/qwen3:14b", 1.0))

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai",
            json={"backend": "ollama", "model": "qwen3:14b"},
        )

    assert resp.status_code == 200, resp.text
    assert applier.calls == [{"backend": "ollama", "model": "qwen3:14b", "tier": "ge-48"}]


@pytest.mark.asyncio
async def test_post_settings_ai_apply_failure_returns_502(
    _base_app,
    _ai_config: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, _pool, _conn = _base_app
    monkeypatch.setattr(settings_ai, "_APPLIER", _FakeApplier(fail=True))
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai",
            json={"backend": "ollama", "model": "qwen3:14b"},
        )

    assert resp.status_code == 502, resp.text
    assert "reverted" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_redetect_returns_settings(
    _base_app,
    _ai_config: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    app, _pool, _conn = _base_app
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    async with _admin_client(app) as client:
        resp = await client.post("/api/settings/ai/redetect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hw_tier"] == "ge-48"
    assert body["candidates_for_tier"][0]["model"] == "qwen3:14b"


@pytest.mark.asyncio
async def test_redetect_requires_admin(_base_app):
    app, _pool, _conn = _base_app

    async with _anon_client(app) as client:
        resp = await client.post("/api/settings/ai/redetect")

    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_dismiss_banner_inserts_event(_base_app):
    app, _pool, conn = _base_app
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai/dismiss-banner",
            json={"banner_kind": "hw-upgrade-available"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert conn.execute.called
    assert "banner dismissed" in conn.execute.call_args[0][4]
    assert "hw-upgrade-available" in conn.execute.call_args[0][4]


@pytest.mark.asyncio
async def test_dismiss_banner_requires_admin(_base_app):
    app, _pool, _conn = _base_app

    async with _anon_client(app) as client:
        resp = await client.post(
            "/api/settings/ai/dismiss-banner",
            json={"banner_kind": "hw-upgrade-available"},
        )

    assert resp.status_code in (401, 403), resp.text
