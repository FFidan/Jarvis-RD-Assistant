"""Tests for GET /api/system/setup-status (A1 setup wizard backend)."""

from __future__ import annotations

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from tests.conftest import FakeRecord, _make_pool_and_conn


def _user_config_rows(
    *,
    setup_completed=False,
) -> list[FakeRecord]:
    rows: list[FakeRecord] = [
        FakeRecord(key="setup.completed", value=setup_completed),
    ]
    return rows


@pytest.fixture()
def _app(monkeypatch):
    # Ensure TELEGRAM_BOT_TOKEN is deterministically absent by default.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama-mock:11434")

    from jarvis_common import require_admin, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # AUTHZ-03 added require_admin to get_setup_status; these tests verify the
    # response logic (the admin gate itself is covered in test_system_authz).
    app.dependency_overrides[require_admin] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_probe(monkeypatch, *, models_ready: bool, downloading=None):
    """Replace ``_probe_ollama`` so setup-status tests don't touch the network."""

    async def _fake_probe():
        return models_ready, list(downloading or [])

    monkeypatch.setattr("paper_ingestion.routers.system._probe_ollama", _fake_probe)


def _install_user_config(
    conn,
    *,
    setup_completed=False,
    telegram_paired: bool = False,
    topics_count: int = 0,
):
    conn.fetch.return_value = _user_config_rows(setup_completed=setup_completed)
    # get_setup_status calls fetchrow twice: topics count then telegram_user_pairings count.
    conn.fetchrow.side_effect = [
        FakeRecord(n=topics_count),
        FakeRecord(n=1 if telegram_paired else 0),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_status_reads_user_config(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn, setup_completed=True, telegram_paired=True, topics_count=3)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["setup_completed"] is True
    assert body["telegram_paired"] is True
    assert body["topics_count"] == 3


@pytest.mark.asyncio
async def test_setup_status_telegram_configured_reads_env(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy:secret")
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["telegram_configured"] is True


@pytest.mark.asyncio
async def test_setup_status_telegram_configured_false_when_env_missing(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["telegram_configured"] is False


@pytest.mark.asyncio
async def test_setup_status_models_ready_true_when_ollama_ok(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=True)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models_ready"] is True
    assert body["models_downloading"] == []


@pytest.mark.asyncio
async def test_setup_status_models_ready_false_on_ollama_error(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models_ready"] is False
    assert body["models_downloading"] == []


@pytest.mark.asyncio
async def test_setup_status_topics_count_from_db(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn, topics_count=17)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["topics_count"] == 17


@pytest.mark.asyncio
async def test_setup_status_telegram_paired_false_when_null(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn, telegram_paired=False)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["telegram_paired"] is False


# ---------------------------------------------------------------------------
# Direct unit tests for _probe_ollama / _models_match helpers
# ---------------------------------------------------------------------------


def test_models_match_true_on_default_install():
    """Default install (qwen3:8b + embedder) must report ready — SYSCHECK-01."""
    from paper_ingestion.routers.system import _models_match

    # The exact set pulled by setup.sh on a default install.
    assert _models_match(["qwen3:8b", "qwen3-embedding:4b"]) is True


def test_models_match_true_all_variants():
    """Any qwen3 chat tag (4b / 8b / 14b) combined with the embedder is ready."""
    from paper_ingestion.routers.system import _models_match

    assert _models_match(["qwen3:8b", "qwen3:4b", "qwen3-embedding:4b"]) is True
    assert _models_match(["qwen3:14b", "qwen3-embedding:4b"]) is True
    assert _models_match(["qwen3:4b", "qwen3-embedding:4b"]) is True


def test_models_match_false_when_embedder_missing():
    """Chat model present but embedder absent → not ready."""
    from paper_ingestion.routers.system import _models_match

    assert _models_match(["qwen3:8b"]) is False


def test_models_match_false_when_chat_missing():
    """Embedder present but no qwen3 chat model → not ready."""
    from paper_ingestion.routers.system import _models_match

    assert _models_match(["qwen3-embedding:4b"]) is False


def test_models_match_false_on_empty():
    from paper_ingestion.routers.system import _models_match

    assert _models_match([]) is False


@pytest.mark.asyncio
async def test_probe_ollama_returns_false_when_unreachable(monkeypatch):
    """_probe_ollama must not raise when Ollama is down."""
    from paper_ingestion.routers import system as system_module

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _BoomClient())

    ready, downloading = await system_module._probe_ollama()
    assert ready is False
    assert downloading == []


@pytest.mark.asyncio
async def test_probe_ollama_reachable_but_incomplete_populates_downloading(monkeypatch):
    """SYSCHECK-02: reachable Ollama with only embedder returns non-empty downloading."""

    from paper_ingestion.routers import system as system_module

    # Flush the TTL cache so our mock is used fresh.
    system_module._ollama_probe_cache._ts = 0.0

    class _FakeResponse:
        status_code = 200

        def json(self):
            # Only the embedder is installed; chat model is missing.
            return {"models": [{"name": "qwen3-embedding:4b"}]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            return _FakeResponse()

    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _FakeClient())

    ready, downloading = await system_module._probe_ollama()
    assert ready is False
    # Only the embedder is installed, so the chat model is the missing piece.
    assert "qwen3 chat model" in downloading


# ---------------------------------------------------------------------------
# model_warnings — Part 2 (F-MODEL-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_status_model_warnings_when_routed_model_not_pulled(_app, monkeypatch):
    """model_warnings is non-empty when LiteLLM routes a model not pulled in Ollama.

    Patches _compute_model_warnings directly so the test client's own
    httpx.AsyncClient is not replaced (replacing system_module.httpx.AsyncClient
    would break the test's own ASGITransport client since httpx is a shared module).
    """
    import paper_ingestion.routers.system as system_module

    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    # Return a canned "not pulled" warning: smart routes to qwen3:8b which is not pulled.
    async def _fake_compute_warnings():
        return ["smart routes to qwen3:8b which is not pulled"]

    monkeypatch.setattr(system_module, "_compute_model_warnings", _fake_compute_warnings)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    warnings = body["model_warnings"]
    assert isinstance(warnings, list)
    assert any("qwen3:8b" in w and "not pulled" in w for w in warnings), (
        f"expected a 'not pulled' warning for qwen3:8b, got: {warnings}"
    )


@pytest.mark.asyncio
async def test_setup_status_model_warnings_empty_when_all_pulled(_app, monkeypatch):
    """model_warnings is [] when every routed Ollama model is already installed.

    Patches _compute_model_warnings directly to return [] (all matched).
    """
    import paper_ingestion.routers.system as system_module

    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=True)

    async def _fake_compute_warnings():
        return []

    monkeypatch.setattr(system_module, "_compute_model_warnings", _fake_compute_warnings)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["model_warnings"] == [], f"expected empty warnings, got: {body['model_warnings']}"


@pytest.mark.asyncio
async def test_setup_status_model_warnings_empty_and_200_when_litellm_down(_app, monkeypatch):
    """model_warnings is [] and endpoint still 200s when LiteLLM is unreachable."""
    import paper_ingestion.services.litellm_config as _lc

    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    async def _deployments_fail():
        raise RuntimeError("LiteLLM /v1/model/info unreachable: connection refused")

    monkeypatch.setattr(_lc, "get_litellm_deployments", _deployments_fail)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["model_warnings"] == [], (
        f"expected empty warnings when LiteLLM is down, got: {body['model_warnings']}"
    )


# ---------------------------------------------------------------------------
# Direct unit tests for _compute_model_warnings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_model_warnings_not_pulled(monkeypatch):
    """_compute_model_warnings returns a warning when a routed model is not installed."""
    import paper_ingestion.services.litellm_config as _lc
    import paper_ingestion.routers.system as system_module

    # Flush the model-warnings TTL cache so this test's mocks are used fresh
    # (the cache is shared across direct _compute_model_warnings tests).
    system_module._model_warnings_cache._ts = 0.0

    async def _fake_deployments():
        return [
            _lc.LiteLLMDeployment.model_validate(
                {
                    "model_name": "smart",
                    "litellm_params": {"model": "ollama_chat/qwen3:8b"},
                    "model_info": {"id": "dep-1", "db_model": True},
                }
            )
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    class _OllamaResponse:
        status_code = 200

        def json(self):
            # Only qwen3:4b is installed; qwen3:8b is missing.
            return {"models": [{"name": "qwen3:4b"}, {"name": "qwen3-embedding:4b"}]}

    class _OllamaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_a, **_k):
            return _OllamaResponse()

    # Patch only inside _compute_model_warnings' httpx call, without touching
    # any transport clients. We do this by monkeypatching the module attribute.
    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _OllamaClient())

    warnings = await system_module._compute_model_warnings()
    assert any("qwen3:8b" in w and "not pulled" in w for w in warnings), (
        f"expected a 'not pulled' warning for qwen3:8b, got: {warnings}"
    )


@pytest.mark.asyncio
async def test_compute_model_warnings_empty_when_all_match(monkeypatch):
    """_compute_model_warnings returns [] when every routed model is installed."""
    import paper_ingestion.services.litellm_config as _lc
    import paper_ingestion.routers.system as system_module

    # Flush the model-warnings TTL cache so this test's mocks are used fresh.
    system_module._model_warnings_cache._ts = 0.0

    async def _fake_deployments():
        return [
            _lc.LiteLLMDeployment.model_validate(
                {
                    "model_name": "smart",
                    "litellm_params": {"model": "ollama_chat/qwen3:8b"},
                    "model_info": {"id": "dep-smart", "db_model": True},
                }
            ),
            _lc.LiteLLMDeployment.model_validate(
                {
                    "model_name": "fast",
                    "litellm_params": {"model": "ollama_chat/qwen3:4b"},
                    "model_info": {"id": "dep-fast", "db_model": True},
                }
            ),
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    class _OllamaResponse:
        status_code = 200

        def json(self):
            return {
                "models": [
                    {"name": "qwen3:8b"},
                    {"name": "qwen3:4b"},
                    {"name": "qwen3-embedding:4b"},
                ]
            }

    class _OllamaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_a, **_k):
            return _OllamaResponse()

    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _OllamaClient())

    warnings = await system_module._compute_model_warnings()
    assert warnings == [], f"expected empty warnings, got: {warnings}"


@pytest.mark.asyncio
async def test_compute_model_warnings_latest_tolerant(monkeypatch):
    """_compute_model_warnings treats 'qwen3:8b' and 'qwen3:8b:latest' as equal (not pulled = False)."""
    import paper_ingestion.services.litellm_config as _lc
    import paper_ingestion.routers.system as system_module

    # Flush the model-warnings TTL cache so this test's mocks are used fresh.
    system_module._model_warnings_cache._ts = 0.0

    async def _fake_deployments():
        return [
            _lc.LiteLLMDeployment.model_validate(
                {
                    "model_name": "smart",
                    # LiteLLM reports with :latest
                    "litellm_params": {"model": "ollama_chat/qwen3:8b:latest"},
                    "model_info": {"id": "dep-smart", "db_model": True},
                }
            )
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    class _OllamaResponse:
        status_code = 200

        def json(self):
            # Ollama stores without :latest
            return {"models": [{"name": "qwen3:8b"}, {"name": "qwen3-embedding:4b"}]}

    class _OllamaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_a, **_k):
            return _OllamaResponse()

    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _OllamaClient())

    warnings = await system_module._compute_model_warnings()
    assert warnings == [], (
        f":latest tolerance: expected no warning when only tag differs, got: {warnings}"
    )


@pytest.mark.asyncio
async def test_compute_model_warnings_excludes_embed_role(monkeypatch):
    """An embed deployment routing an un-pulled model yields NO warning.

    embed is deliberately excluded from the warning loop (only smart/fast are
    checked): embed is dimension-locked to the Qdrant collection and is not a
    user-switchable role, so a routed-but-unpulled embed is not an actionable
    setup warning the way a smart/fast mismatch is.
    """
    import paper_ingestion.services.litellm_config as _lc
    import paper_ingestion.routers.system as system_module

    # Flush the model-warnings TTL cache so this test's mocks are used fresh.
    system_module._model_warnings_cache._ts = 0.0

    async def _fake_deployments():
        return [
            _lc.LiteLLMDeployment.model_validate(
                {
                    "model_name": "embed",
                    # embed routes a model that is NOT installed in Ollama.
                    "litellm_params": {"model": "ollama/qwen3-embedding:0.6b"},
                    "model_info": {"id": "dep-embed", "db_model": True},
                }
            )
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    class _OllamaResponse:
        status_code = 200

        def json(self):
            # Only a different embedder is installed; the routed embed is absent.
            return {"models": [{"name": "qwen3-embedding:4b"}]}

    class _OllamaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, *_a, **_k):
            return _OllamaResponse()

    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _OllamaClient())

    warnings = await system_module._compute_model_warnings()
    assert warnings == [], (
        f"embed is excluded from warnings (dimension-locked role); got: {warnings}"
    )


# ---------------------------------------------------------------------------
# _strip_latest edge cases (FX9.2)
# ---------------------------------------------------------------------------


def test_strip_latest_edge_cases():
    """_strip_latest only removes a trailing ':latest', leaving everything else intact."""
    from paper_ingestion.routers.system import _strip_latest

    assert _strip_latest(":latest") == ""
    assert _strip_latest("") == ""
    assert _strip_latest("a:b:latest") == "a:b"
