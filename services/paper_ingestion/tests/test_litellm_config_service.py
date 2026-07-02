"""Tests for LiteLLM model delivery via the admin DB (/model/new + /model/delete).

Pins the delivery contract of ``update_litellm_model`` / ``ensure_smart_fallback``:

- replacement deployments are created with POST /model/new and superseded DB
  deployments removed with POST /model/delete (create-first, delete-after)
- num_ctx / think ride TOP-LEVEL in litellm_params (the only placement Ollama
  honours through LiteLLM); legacy extra_body values are lifted on carry
- no-op detection: an alias already routing the requested model with the same
  effective params is left alone (False return, zero admin calls)
- cloud no-op: /v1/model/info pops api_key, so the cloud signature's key leg
  is the process-local fingerprint of the last delivery — repeats no-op, a
  rotated key re-delivers, and a restart re-delivers once (empty cache)
- decrypted cloud keys never appear in raised error text (redacted to ***)
- the embed alias is dimension-locked: re-selecting its routed model is a
  no-op, re-routing it is refused
- a failed stale-deployment delete rolls back the just-created deployment
- smart-fallback mirrors cloud semantics for cloud fast models and pins to
  the static pulled default when the provider key is missing
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from paper_ingestion.services import litellm_config as litellm_config_module
from paper_ingestion.services.litellm_config import (
    ROLE_TO_ALIAS,
    ensure_smart_fallback,
    update_litellm_model,
)
from tests.conftest import _make_pool_and_conn

LITELLM = "http://litellm:4000"


@pytest.fixture(autouse=True)
def _reset_cloud_delivery_state():
    """The cloud no-op fingerprint cache and warn-once sets are process-local
    module state — clear them so tests don't see each other's deliveries."""
    litellm_config_module._CLOUD_DELIVERED_FINGERPRINTS.clear()
    litellm_config_module._FALLBACK_KEYLESS_WARNED.clear()
    yield
    litellm_config_module._CLOUD_DELIVERED_FINGERPRINTS.clear()
    litellm_config_module._FALLBACK_KEYLESS_WARNED.clear()


def _entry(
    alias: str,
    params: dict[str, Any],
    *,
    dep_id: str = "dep-1",
    db_model: bool = True,
) -> dict[str, Any]:
    return {
        "model_name": alias,
        "litellm_params": params,
        "model_info": {"id": dep_id, "db_model": db_model},
    }


def _mock_model_info(entries: list[dict[str, Any]]) -> None:
    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(200, json={"data": entries})
    )


def _last_payload(route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


def test_role_to_alias_covers_all_llm_keys():
    """ROLE_TO_ALIAS should map all llm.* config keys."""
    assert "llm.smart_model" in ROLE_TO_ALIAS
    assert "llm.fast_model" in ROLE_TO_ALIAS
    assert "llm.embed_model" in ROLE_TO_ALIAS


@pytest.mark.asyncio
async def test_update_unknown_role_returns_false():
    """A config key not in ROLE_TO_ALIAS returns False without any admin call."""
    with respx.mock:  # no routes mocked — any HTTP call would error the test
        assert await update_litellm_model("ui.page_size", "10") is False


@pytest.mark.asyncio
async def test_update_rejects_invalid_model_name():
    """Shell metacharacters / path traversal never reach the admin API."""
    with pytest.raises(ValueError, match="disallowed characters"):
        await update_litellm_model("llm.smart_model", "bad;rm -rf /")


@respx.mock
@pytest.mark.asyncio
async def test_update_replaces_db_deployment(monkeypatch):
    """Model switch = POST /model/new (replacement) + POST /model/delete (old id)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    _mock_model_info(
        [_entry("smart", {"model": "ollama_chat/mistral-nemo", "api_base": "http://ollama:11434"})]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={"message": "deleted"})
    )

    result = await update_litellm_model("llm.smart_model", "qwen3:4b")

    assert result is True
    payload = _last_payload(new_route)
    assert payload["model_name"] == "smart"
    assert payload["litellm_params"]["model"] == "ollama_chat/qwen3:4b"
    assert payload["litellm_params"]["api_base"] == "http://ollama:11434"
    assert _last_payload(delete_route) == {"id": "dep-1"}


@respx.mock
@pytest.mark.asyncio
async def test_update_same_model_is_noop():
    """Alias already routing the requested model → False, zero admin writes."""
    _mock_model_info(
        [_entry("smart", {"model": "ollama_chat/mistral-nemo", "api_base": "http://ollama:11434"})]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await update_litellm_model("llm.smart_model", "mistral-nemo")

    assert result is False
    assert not new_route.called
    assert not delete_route.called


@respx.mock
@pytest.mark.asyncio
async def test_update_strips_latest_tag_before_compare():
    """'mistral-nemo:latest' equals 'mistral-nemo' (Ollama implicit tag)."""
    _mock_model_info([_entry("smart", {"model": "ollama_chat/mistral-nemo"})])
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))

    assert await update_litellm_model("llm.smart_model", "mistral-nemo:latest") is False
    assert not new_route.called


@respx.mock
@pytest.mark.asyncio
async def test_update_preserves_non_ollama_prefix_and_api_base():
    """Bare model name inherits the existing non-cloud prefix (vLLM spike, A6)."""
    _mock_model_info(
        [
            _entry(
                "smart",
                {"model": "openai/Qwen/Qwen3-8B-AWQ", "api_base": "http://vllm:8080/v1"},
            )
        ]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    result = await update_litellm_model("llm.smart_model", "gpt-4-turbo")

    assert result is True
    params = _last_payload(new_route)["litellm_params"]
    assert params["model"] == "openai/gpt-4-turbo"
    # The vLLM transport api_base is carried, NOT overwritten with the Ollama URL.
    assert params["api_base"] == "http://vllm:8080/v1"


@respx.mock
@pytest.mark.asyncio
async def test_update_preserves_existing_num_ctx_when_disabling_think():
    """think=False merges with the deployment's num_ctx — both TOP-LEVEL."""
    _mock_model_info(
        [_entry("smart", {"model": "ollama_chat/qwen3:14b", "num_ctx": 8192}, dep_id="old-14b")]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-14b"})
    )
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await update_litellm_model(
        "llm.smart_model",
        "qwen3:14b",
        machine_id="host-rtx5060",
        thinking_disabled=True,
    )

    assert result is True
    params = _last_payload(new_route)["litellm_params"]
    assert params["num_ctx"] == 8192
    assert params["think"] is False
    # Top-level placement is the contract: nested extra_body is ignored by Ollama.
    assert "extra_body" not in params
    assert _last_payload(delete_route) == {"id": "old-14b"}


@respx.mock
@pytest.mark.asyncio
async def test_update_lifts_legacy_extra_body_values():
    """Deployments created by the old delivery path carried extra_body — lift on carry."""
    _mock_model_info(
        [
            _entry(
                "smart",
                {"model": "ollama_chat/qwen3:14b", "extra_body": {"num_ctx": 8192, "think": False}},
            )
        ]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    result = await update_litellm_model("llm.smart_model", "qwen3:8b")

    assert result is True
    params = _last_payload(new_route)["litellm_params"]
    assert params["model"] == "ollama_chat/qwen3:8b"
    assert params["num_ctx"] == 8192
    assert params["think"] is False
    assert "extra_body" not in params


@respx.mock
@pytest.mark.asyncio
async def test_update_explicit_reenable_removes_think():
    """thinking_disabled=False removes only the think flag, keeping num_ctx."""
    _mock_model_info(
        [_entry("smart", {"model": "ollama_chat/qwen3:14b", "num_ctx": 8192, "think": False})]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    result = await update_litellm_model(
        "llm.smart_model",
        "qwen3:14b",
        machine_id="host-rtx5060",
        thinking_disabled=False,
    )

    assert result is True
    params = _last_payload(new_route)["litellm_params"]
    assert "think" not in params
    assert params["num_ctx"] == 8192


@respx.mock
@pytest.mark.asyncio
async def test_update_pending_num_ctx_override_wins():
    """An explicit num_ctx kwarg (pending settings write) overrides the carried value."""
    _mock_model_info([_entry("smart", {"model": "ollama_chat/qwen3:8b", "num_ctx": 8192})])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    result = await update_litellm_model(
        "llm.smart_model", "qwen3:8b", machine_id="host-a", num_ctx=16384
    )

    assert result is True
    assert _last_payload(new_route)["litellm_params"]["num_ctx"] == 16384


@respx.mock
@pytest.mark.asyncio
async def test_local_delivery_syncs_system_num_ctx_row_and_invalidates_cache(monkeypatch):
    """A successful Ollama delivery that carried a num_ctx writes the system
    ``llm.{role}_num_ctx`` row (the prompt-budget source of truth) AND drops the
    effective-context cache — so a reconciler / model-change delivery cannot
    leave the budget reading a stale window across a fleet."""
    from tests.conftest import _make_pool_and_conn

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    _mock_model_info([_entry("smart", {"model": "ollama_chat/qwen3:8b", "num_ctx": 8192})])
    respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    pool, conn = _make_pool_and_conn(fetchrow_return=None)
    invalidated: list[bool] = []
    monkeypatch.setattr(
        litellm_config_module,
        "invalidate_effective_num_ctx_cache",
        lambda: invalidated.append(True),
    )

    result = await update_litellm_model(
        "llm.smart_model", "qwen3:8b", db_pool=pool, machine_id="host-a", num_ctx=4096
    )

    assert result is True
    # The system row was upserted with the DELIVERED value (4096), not the
    # carried 8192, keyed by role (smart).
    upserts = [
        c
        for c in conn.execute.await_args_list
        if "user_config" in c.args[0] and "llm.smart_num_ctx" in c.args
    ]
    assert len(upserts) == 1
    assert 4096 in upserts[0].args
    assert invalidated == [True]


@respx.mock
@pytest.mark.asyncio
async def test_local_noop_delivery_does_not_touch_system_num_ctx_row(monkeypatch):
    """When the alias already routes the requested model with the same window,
    no delivery happens — and the system row / cache are left untouched."""
    from tests.conftest import _make_pool_and_conn

    _mock_model_info([_entry("smart", {"model": "ollama_chat/qwen3:8b", "num_ctx": 4096})])
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))

    # fetchrow_return=None: the per-machine thinking_disabled read resolves to
    # False so the routing signature matches the carried deployment exactly.
    pool, conn = _make_pool_and_conn(fetchrow_return=None)
    invalidated: list[bool] = []
    monkeypatch.setattr(
        litellm_config_module,
        "invalidate_effective_num_ctx_cache",
        lambda: invalidated.append(True),
    )

    result = await update_litellm_model(
        "llm.smart_model", "qwen3:8b", db_pool=pool, machine_id="host-a", num_ctx=4096
    )

    assert result is False
    assert not new_route.called
    upserts = [c for c in conn.execute.await_args_list if "llm.smart_num_ctx" in c.args]
    assert not upserts
    assert invalidated == []


@respx.mock
@pytest.mark.asyncio
async def test_fresh_creation_seeds_bootstrap_defaults(monkeypatch):
    """No existing deployment (post-de-seed bootstrap) → tuned defaults are seeded."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    _mock_model_info([_entry("embed", {"model": "ollama/qwen3-embedding:4b"}, db_model=False)])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-smart"})
    )
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await update_litellm_model("llm.smart_model", "qwen3:8b")

    assert result is True
    params = _last_payload(new_route)["litellm_params"]
    assert params["model"] == "ollama_chat/qwen3:8b"
    assert params["api_base"] == "http://ollama:11434"
    assert params["temperature"] == 0.2
    assert params["num_ctx"] == 8192
    assert params["think"] is False
    assert params["timeout"] == 300
    assert not delete_route.called  # nothing to supersede


@respx.mock
@pytest.mark.asyncio
async def test_embed_same_model_is_noop():
    """Re-selecting the YAML-routed embed model is truthfully 'nothing to change'."""
    _mock_model_info([_entry("embed", {"model": "ollama/qwen3-embedding:4b"}, db_model=False)])
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))

    assert await update_litellm_model("llm.embed_model", "qwen3-embedding:4b") is False
    assert not new_route.called


@respx.mock
@pytest.mark.asyncio
async def test_embed_reroute_refused():
    """The embed alias is dimension-locked; re-routing it is an explicit error."""
    _mock_model_info([_entry("embed", {"model": "ollama/qwen3-embedding:4b"}, db_model=False)])
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(RuntimeError, match="dimension-locked"):
        await update_litellm_model("llm.embed_model", "mxbai-embed-large")
    assert not new_route.called


@respx.mock
@pytest.mark.asyncio
async def test_yaml_stacked_alias_warns_but_delivers(caplog):
    """A stale YAML smart (upgrade path) cannot be deleted — warn loudly, deliver anyway."""
    import logging

    _mock_model_info(
        [_entry("smart", {"model": "ollama_chat/qwen3:8b"}, db_model=False, dep_id="yaml-1")]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={})
    )

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.litellm_config"):
        result = await update_litellm_model("llm.smart_model", "qwen3:14b")

    assert result is True
    assert new_route.called
    assert not delete_route.called  # YAML deployments are not deletable
    assert any("STACK" in r.message for r in caplog.records)


@respx.mock
@pytest.mark.asyncio
async def test_delete_failure_rolls_back_new_deployment():
    """Failed stale-deployment cleanup rolls the just-created deployment back."""
    _mock_model_info([_entry("smart", {"model": "ollama_chat/qwen3:8b"}, dep_id="old-1")])
    respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "new-1"})
    )
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),  # delete old-1 fails
            httpx.Response(200, json={}),  # rollback delete new-1 succeeds
        ]
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await update_litellm_model("llm.smart_model", "qwen3:14b")

    deleted_ids = [json.loads(c.request.content)["id"] for c in delete_route.calls]
    assert deleted_ids == ["old-1", "new-1"]


@respx.mock
@pytest.mark.asyncio
async def test_model_info_failure_raises():
    """Routing-state read failures surface as RuntimeError (fail-closed upstream)."""
    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(500, json={"error": "router not loaded"})
    )

    with pytest.raises(RuntimeError, match="/v1/model/info failed"):
        await update_litellm_model("llm.smart_model", "qwen3:8b")


# ---------------------------------------------------------------------------
# Cloud path: fingerprinted no-op + key redaction
# ---------------------------------------------------------------------------

_CLOUD_MODEL = "anthropic/claude-haiku-4-5"
_KEY_PATCH_TARGET = "paper_ingestion.services.litellm_config.get_provider_api_key"


@respx.mock
@pytest.mark.asyncio
async def test_cloud_update_delivers_then_noops():
    """Reconciler-shaped repeat: the first cloud delivery goes out, the repeat no-ops.

    Without the fingerprinted no-op the 30 s reconciler would re-deliver the
    cloud alias forever — deployment-id churn, router cooldown resets, and the
    plaintext key re-transmitted on every pass.
    """
    _mock_model_info([_entry("smart", {"model": _CLOUD_MODEL}, dep_id="dep-cloud")])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "cloud-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value="sk-ant-test")):
        first = await update_litellm_model("llm.smart_model", _CLOUD_MODEL, db_pool=object())
        second = await update_litellm_model("llm.smart_model", _CLOUD_MODEL, db_pool=object())

    assert first is True  # boot redelivery: cache is empty after restart
    assert second is False  # same (model, think, key fingerprint) → no-op
    assert new_route.call_count == 1
    params = json.loads(new_route.calls[0].request.content)["litellm_params"]
    assert params["api_key"] == "sk-ant-test"
    # Ollama-only / local-transport params never leak onto a cloud deployment.
    assert "api_base" not in params
    assert "num_ctx" not in params


@respx.mock
@pytest.mark.asyncio
async def test_cloud_key_rotation_redelivers():
    """A rotated provider key changes the fingerprint → the next call delivers it."""
    _mock_model_info([_entry("smart", {"model": _CLOUD_MODEL}, dep_id="dep-cloud")])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "cloud-1"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value="sk-ant-old")):
        assert await update_litellm_model("llm.smart_model", _CLOUD_MODEL, db_pool=object()) is True
    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value="sk-ant-new")):
        rotated = await update_litellm_model("llm.smart_model", _CLOUD_MODEL, db_pool=object())

    assert rotated is True
    assert new_route.call_count == 2
    params = json.loads(new_route.calls[1].request.content)["litellm_params"]
    assert params["api_key"] == "sk-ant-new"


@respx.mock
@pytest.mark.asyncio
async def test_cloud_error_text_redacts_api_key():
    """FastAPI 422s echo the submitted body — the raised error must not carry the key."""
    secret = "sk-ant-secret-1234567890"
    _mock_model_info([])
    respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(
            422,
            json={"detail": [{"loc": ["body"], "input": {"api_key": secret}}]},
        )
    )

    with (
        patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value=secret)),
        pytest.raises(RuntimeError, match="model/new failed") as exc_info,
    ):
        await update_litellm_model("llm.smart_model", _CLOUD_MODEL, db_pool=object())

    message = str(exc_info.value)
    assert secret not in message
    assert "***" in message


# ---------------------------------------------------------------------------
# ensure_smart_fallback
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_creates_deployment():
    """Missing smart-fallback → created with the fast-tier params + timeout 120."""
    _mock_model_info([_entry("embed", {"model": "ollama/qwen3-embedding:4b"}, db_model=False)])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "fb-1"})
    )

    result = await ensure_smart_fallback("qwen3:4b")

    assert result is True
    payload = _last_payload(new_route)
    assert payload["model_name"] == "smart-fallback"
    params = payload["litellm_params"]
    assert params["model"] == "ollama_chat/qwen3:4b"
    assert params["timeout"] == 120
    assert params["num_ctx"] == 4096
    assert params["think"] is False
    assert params["temperature"] == 0.1


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_existing_is_noop():
    """smart-fallback already routing the fast model → False, no admin writes."""
    _mock_model_info([_entry("smart-fallback", {"model": "ollama_chat/qwen3:4b", "timeout": 120})])
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))

    assert await ensure_smart_fallback("qwen3:4b") is False
    assert not new_route.called


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_cloud_fast_model_carries_key():
    """A cloud fast model mirrors the cloud delivery semantics for the fallback group:
    provider key carried, no api_base/num_ctx/think, fallback timeout kept."""
    _mock_model_info([])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "fb-cloud"})
    )

    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value="sk-ant-test")):
        result = await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object())

    assert result is True
    payload = _last_payload(new_route)
    assert payload["model_name"] == "smart-fallback"
    params = payload["litellm_params"]
    assert params["model"] == _CLOUD_MODEL
    assert params["api_key"] == "sk-ant-test"
    assert params["timeout"] == 120
    assert params["temperature"] == 0.1
    # Ollama-only params would break (or be junk on) a cloud deployment.
    assert "api_base" not in params
    assert "num_ctx" not in params
    assert "think" not in params


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_cloud_noop_and_key_rotation():
    """Cloud fallback no-ops on repeat (fingerprint match) but re-delivers a rotated key."""
    _mock_model_info(
        [_entry("smart-fallback", {"model": _CLOUD_MODEL, "timeout": 120}, dep_id="fb-1")]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "fb-2"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value="sk-ant-old")):
        assert await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object()) is True
        assert await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object()) is False
    assert new_route.call_count == 1

    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value="sk-ant-new")):
        assert await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object()) is True
    assert new_route.call_count == 2
    assert json.loads(new_route.calls[1].request.content)["litellm_params"]["api_key"] == (
        "sk-ant-new"
    )


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_cloud_missing_key_pins_static_default(monkeypatch, caplog):
    """No provider key → the fallback pins to the static pulled default (with the
    full ollama bootstrap params) instead of a deployment guaranteed to fail
    exactly when smart fails; the warning fires once, not every 30 s pass."""
    import logging

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    _mock_model_info([])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "fb-pinned"})
    )

    with (
        caplog.at_level(logging.WARNING, logger="paper_ingestion.services.litellm_config"),
        patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value=None)),
    ):
        first = await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object())
        second = await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object())

    assert first is True
    assert second is True  # model_info mock never shows the pinned deployment
    params = _last_payload(new_route)["litellm_params"]
    assert params["model"] == "ollama_chat/qwen3:4b"
    assert params["api_base"] == "http://ollama:11434"
    assert params["num_ctx"] == 4096
    assert params["think"] is False
    assert "api_key" not in params
    pin_warnings = [r for r in caplog.records if "pinning" in r.message]
    assert len(pin_warnings) == 1  # warn-once: this runs every reconciler pass


# ---------------------------------------------------------------------------
# LiteLLM delivery-failure detail hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_litellm_runtime_update_strips_upstream_body_from_detail(monkeypatch, caplog):
    """A delivery RuntimeError exposes alias+status in the 400 detail, not the raw body.

    The full error (including upstream body) must appear in the server ERROR log
    while the HTTP detail returned to the client is truncated to alias + HTTP status
    so raw LiteLLM internals never leak over the API boundary.

    The No-DB carve-out path (pending_restart → 200) is unaffected by this change.
    """
    import logging

    from fastapi import HTTPException

    from paper_ingestion.services.config_write import _apply_litellm_runtime_update

    raw_body = '{"error": "invalid key", "secret": "sk-abc123"}'
    full_error = f"LiteLLM /model/new failed for alias 'smart': HTTP 400 {raw_body}"

    async def _failing_update_fn(*args, **kwargs):
        raise RuntimeError(full_error)

    monkeypatch.setenv("LITELLM_BASE_URL", LITELLM)
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"value": "ollama_chat/qwen3:8b", "encrypted_value": None}

    with caplog.at_level(logging.ERROR, logger="paper_ingestion.services.config_write"):
        with pytest.raises(HTTPException) as exc_info:
            await _apply_litellm_runtime_update(
                db_pool=pool,
                key="llm.smart_model",
                value="ollama_chat/qwen3:8b",
                update_litellm_model_fn=_failing_update_fn,
            )

    exc = exc_info.value
    assert exc.status_code == 400

    detail = str(exc.detail)
    assert "alias 'smart'" in detail
    assert "HTTP 400" in detail
    assert raw_body not in detail, "raw upstream body must not appear in HTTP detail"
    assert "secret" not in detail, "secrets in upstream body must not leak via HTTP detail"

    assert any(raw_body in r.message for r in caplog.records if r.levelno == logging.ERROR), (
        "full error including upstream body must appear in server ERROR log"
    )


# ---------------------------------------------------------------------------
# ensure_smart_fallback — stale-sibling deletion when a matching deployment already exists
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_deletes_stale_sibling_when_match_exists():
    """A matching smart-fallback PLUS a stale non-matching sibling → sibling deleted,
    matching deployment survives, returns False (no new delivery needed)."""
    _mock_model_info(
        [
            _entry(
                "smart-fallback",
                {"model": "ollama_chat/qwen3:4b", "timeout": 120},
                dep_id="match-1",
            ),
            _entry("smart-fallback", {"model": "ollama_chat/old-model:7b"}, dep_id="stale-1"),
        ]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={"message": "deleted"})
    )

    result = await ensure_smart_fallback("qwen3:4b")

    assert result is False
    assert not new_route.called
    assert delete_route.call_count == 1
    deleted_id = json.loads(delete_route.calls.last.request.content)["id"]
    assert deleted_id == "stale-1"


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_match_only_no_delete():
    """A single matching deployment with no stale siblings → no delete, no new delivery."""
    _mock_model_info(
        [
            _entry(
                "smart-fallback",
                {"model": "ollama_chat/qwen3:4b", "timeout": 120},
                dep_id="match-1",
            )
        ]
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(return_value=httpx.Response(200, json={}))
    delete_route = respx.post(f"{LITELLM}/model/delete").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await ensure_smart_fallback("qwen3:4b")

    assert result is False
    assert not new_route.called
    assert not delete_route.called


@respx.mock
@pytest.mark.asyncio
async def test_migrated_reconcile_set_does_not_flap_on_second_pass(monkeypatch):
    """Two consecutive reconcile passes over a migrated smart/fast/smart-fallback
    set deliver nothing the second time.

    A guard that recognised only ``ollama/`` would treat every ``ollama_chat/``
    deployment as foreign — re-emitting the chat prefix and never matching its
    own prior delivery, so each pass would re-create the deployment forever.
    Pass 1 starts from an empty proxy and creates all three; the created
    deployments are fed back as the live state for pass 2, which must be a no-op.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")

    deployments: list[dict[str, Any]] = []
    respx.get(f"{LITELLM}/v1/model/info").mock(
        side_effect=lambda request: httpx.Response(200, json={"data": deployments})
    )
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "dep-new"})
    )
    respx.post(f"{LITELLM}/model/delete").mock(return_value=httpx.Response(200, json={}))

    async def _reconcile() -> list[bool]:
        return [
            await update_litellm_model("llm.smart_model", "qwen3:8b"),
            await update_litellm_model("llm.fast_model", "qwen3:4b"),
            await ensure_smart_fallback("qwen3:4b"),
        ]

    first = await _reconcile()
    assert first == [True, True, True]
    # Capture what pass 1 delivered and present it as the now-live proxy state.
    deployments = [
        _entry(
            json.loads(call.request.content)["model_name"],
            json.loads(call.request.content)["litellm_params"],
            dep_id=f"dep-{idx}",
        )
        for idx, call in enumerate(new_route.calls)
    ]
    assert {d["litellm_params"]["model"] for d in deployments} == {
        "ollama_chat/qwen3:8b",
        "ollama_chat/qwen3:4b",
    }
    calls_after_first = new_route.call_count

    second = await _reconcile()
    assert second == [False, False, False]
    assert new_route.call_count == calls_after_first  # no second-pass delivery


@respx.mock
@pytest.mark.asyncio
async def test_ensure_smart_fallback_missing_key_guard_intact():
    """Missing cloud key still pins to the static default — the keyless guard is unaffected."""
    _mock_model_info([])
    new_route = respx.post(f"{LITELLM}/model/new").mock(
        return_value=httpx.Response(200, json={"model_id": "fb-1"})
    )

    with patch(_KEY_PATCH_TARGET, new=AsyncMock(return_value=None)):
        result = await ensure_smart_fallback(_CLOUD_MODEL, db_pool=object())

    assert result is True
    params = _last_payload(new_route)["litellm_params"]
    assert params["model"] == "ollama_chat/qwen3:4b"
    assert "api_key" not in params


# ---------------------------------------------------------------------------
# Typed deployment parsing: malformed elements are skipped, well-formed parse
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_get_litellm_deployments_skips_malformed_logs_warning(caplog):
    """A deployment element missing model_name is logged as WARNING and skipped;
    a well-formed element is returned as a typed LiteLLMDeployment."""
    import logging

    from paper_ingestion.services.litellm_config import LiteLLMDeployment, get_litellm_deployments

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    # malformed: model_name missing
                    {
                        "litellm_params": {"model": "ollama_chat/bad"},
                        "model_info": {"id": "bad-1", "db_model": True},
                    },
                    # well-formed
                    {
                        "model_name": "smart",
                        "litellm_params": {"model": "ollama_chat/qwen3:8b"},
                        "model_info": {"id": "ok-1", "db_model": True},
                    },
                ]
            },
        )
    )

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.litellm_config"):
        result = await get_litellm_deployments()

    assert len(result) == 1
    dep = result[0]
    assert isinstance(dep, LiteLLMDeployment)
    assert dep.model_name == "smart"
    assert dep.litellm_params["model"] == "ollama_chat/qwen3:8b"
    assert dep.model_info is not None
    assert dep.model_info.id == "ok-1"
    assert dep.model_info.db_model is True
    # The malformed element must produce a WARNING
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed" in t.lower() or "skip" in t.lower() for t in warning_texts)


@respx.mock
@pytest.mark.asyncio
async def test_get_litellm_deployments_keeps_deployment_with_null_model_info():
    """A deployment with an explicit null model_info/litellm_params is KEPT, not dropped.

    The pre-typed code tolerated null via ``entry.get(...) or {}``; the null-coercing
    validator preserves that so a functional deployment (valid model_name) is not lost.
    """
    from paper_ingestion.services.litellm_config import get_litellm_deployments

    respx.get(f"{LITELLM}/v1/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"model_name": "smart", "litellm_params": None, "model_info": None}]},
        )
    )

    result = await get_litellm_deployments()

    assert len(result) == 1
    assert result[0].model_name == "smart"
    assert result[0].litellm_params == {}
    assert result[0].model_info.id == ""


async def test_parse_model_target_strips_latest_splits_cloud_and_validates():
    from paper_ingestion.services.litellm_config import _parse_model_target

    local = _parse_model_target("mistral-nemo:latest")
    assert local.new_name == "mistral-nemo"
    assert local.suffix == "mistral-nemo"
    assert local.cloud_provider is None

    cloud = _parse_model_target("gemini/gemini-1.5-pro")
    assert cloud.cloud_provider == "google"
    assert cloud.suffix == "gemini-1.5-pro"
    assert cloud.new_name == "gemini/gemini-1.5-pro"

    with pytest.raises(ValueError):
        _parse_model_target("../../etc/passwd")


# ---------------------------------------------------------------------------
# _key_fingerprint: PBKDF2-keyed delivery-change identity
# ---------------------------------------------------------------------------


def _fingerprint_with_secret(
    monkeypatch: pytest.MonkeyPatch, api_key: str | None, secret: str | None
) -> str:
    """Compute _key_fingerprint with JARVIS_CONFIG_KEY set to *secret* (None = unset)."""
    from jarvis_common.settings import get_secrets_settings

    if secret is None:
        monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
        monkeypatch.delenv("JARVIS_CONFIG_KEY_FILE", raising=False)
    else:
        monkeypatch.setenv("JARVIS_CONFIG_KEY", secret)
    get_secrets_settings.cache_clear()
    return litellm_config_module._key_fingerprint(api_key)


def test_key_fingerprint_differs_by_config_secret(monkeypatch):
    """The same api_key keyed under two config secrets yields distinct fingerprints."""
    fp_a = _fingerprint_with_secret(monkeypatch, "sk-provider-key", "secret-alpha")
    fp_b = _fingerprint_with_secret(monkeypatch, "sk-provider-key", "secret-bravo")
    assert fp_a != fp_b


def test_key_fingerprint_stable_for_same_key_and_secret(monkeypatch):
    """A fixed (api_key, secret) pair maps to a stable 16-hex-char fingerprint."""
    fp_first = _fingerprint_with_secret(monkeypatch, "sk-provider-key", "secret-alpha")
    fp_second = _fingerprint_with_secret(monkeypatch, "sk-provider-key", "secret-alpha")
    assert fp_first == fp_second
    assert len(fp_first) == 16
    assert all(c in "0123456789abcdef" for c in fp_first)


def test_key_fingerprint_stable_without_config_key(monkeypatch):
    """No config secret (fallback salt) still yields a stable non-empty fingerprint."""
    fp_first = _fingerprint_with_secret(monkeypatch, "sk-provider-key", None)
    fp_second = _fingerprint_with_secret(monkeypatch, "sk-provider-key", None)
    assert fp_first == fp_second
    assert len(fp_first) == 16


@pytest.mark.parametrize("empty", ["", None])
def test_key_fingerprint_empty_for_missing_key(monkeypatch, empty):
    """No api_key → empty fingerprint regardless of the configured secret."""
    assert _fingerprint_with_secret(monkeypatch, empty, "secret-alpha") == ""
