"""Contract tests coupling the num_ctx slider, LiteLLM delivery, and prompt budgets.

One context number: a successful PUT of a per-machine ``llm.<machine>.<role>_num_ctx``
key must (a) land in the live LiteLLM deployment (``litellm_params.num_ctx`` is
TOP-LEVEL — the only placement Ollama honors) and (b) be returned by the
system-scoped budget reader ``jarvis_common.effective_num_ctx``. A failed
delivery must leave the budget untouched (fail-closed), and the write-path
validator must reject absurd values with HTTP 400.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from jarvis_common import effective_num_ctx, invalidate_effective_num_ctx_cache
from jarvis_common.testing import SharedConnPool

from paper_ingestion.services.model_prefixes import is_local_ollama, strip_ollama_prefix

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_MACHINE_KEY = "llm.ctx-contract-host.smart_num_ctx"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def ctx_client(contract_conn, contract_two_users):
    """ASGI client on the real per-test transaction, admin gate bypassed.

    Mirrors the settings-contract fixture: SharedConnPool serves both the
    Depends(get_db_pool) path and request.app.state.db_pool, and require_admin
    is patched in the router namespace because set_config calls it directly.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    async def _allow_all(request=None) -> None:  # noqa: ARG001
        return None

    shared = SharedConnPool(contract_conn)
    _orig_require_admin = _settings_mod.require_admin
    _settings_mod.require_admin = _allow_all
    app.state.limiter.enabled = False
    invalidate_effective_num_ctx_cache()
    try:
        with (
            patch_app_state(app, {"db_pool": shared, "http_client": AsyncMock()}),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    verify_api_key: lambda: None,
                    require_admin: _allow_all,
                },
            ),
        ):
            async with make_contract_client(app, contract_two_users.cookie_a) as client:
                yield client
    finally:
        _settings_mod.require_admin = _orig_require_admin
        app.state.limiter.enabled = True
        invalidate_effective_num_ctx_cache()


async def _seed_system_row(conn, key: str, value) -> None:
    await conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES (NULL, $1, $2::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = $2::jsonb""",
        key,
        value,
    )


def _local_smart_deployments(deployments: list) -> list:
    return [
        d
        for d in deployments
        if d.model_name == "smart" and is_local_ollama(str(d.litellm_params.get("model", "")))
    ]


@pytest.mark.integration
async def test_num_ctx_write_delivers_and_updates_budget_reader(
    contract_conn, ctx_client, monkeypatch
):
    """PUT num_ctx → live LiteLLM deployment AND the budget reader carry the same number.

    Seeds llm.smart_model with the model LiteLLM currently routes for ``smart``
    so the delivery only changes num_ctx, and best-effort restores the prior
    value afterwards (the LiteLLM admin DB outlives the test transaction).
    The deployment's own api_base is pinned for the duration: the test runs the
    delivery in the host process, whose env-derived Ollama URL is not reachable
    from inside the LiteLLM container.
    """
    from paper_ingestion.services.litellm_config import (
        get_litellm_deployments,
        update_litellm_model,
    )

    try:
        deployments = await get_litellm_deployments()
    except RuntimeError as exc:
        pytest.skip(f"live LiteLLM not reachable in this environment: {exc}")
    before = _local_smart_deployments(deployments)
    if not before:
        pytest.skip(
            "live LiteLLM has no local (ollama/ or ollama_chat/) smart deployment to retune"
        )
    prior_params = before[0].litellm_params
    model_id = strip_ollama_prefix(str(prior_params["model"]))
    prior_num_ctx = prior_params.get("num_ctx")
    prior_api_base = prior_params.get("api_base")
    if prior_api_base:
        monkeypatch.setenv("OLLAMA_BASE_URL", str(prior_api_base))

    await _seed_system_row(contract_conn, "llm.smart_model", model_id)
    invalidate_effective_num_ctx_cache()

    try:
        resp = await ctx_client.put(
            f"/api/config/{_MACHINE_KEY}",
            json={"key": _MACHINE_KEY, "value": 2048},
        )
        assert resp.status_code == 200, f"num_ctx PUT failed: {resp.json()}"

        delivered = _local_smart_deployments(await get_litellm_deployments())
        assert delivered, "smart deployment disappeared after delivery"
        assert any(d.litellm_params.get("num_ctx") == 2048 for d in delivered), (
            f"LiteLLM smart deployment must carry num_ctx=2048; "
            f"got {[d.litellm_params.get('num_ctx') for d in delivered]}"
        )

        row = await contract_conn.fetchrow(
            "SELECT value FROM user_config WHERE key = 'llm.smart_num_ctx' AND user_id IS NULL"
        )
        assert row is not None, "delivery success must write the system llm.smart_num_ctx row"
        assert row["value"] == 2048

        shared = SharedConnPool(contract_conn)
        invalidate_effective_num_ctx_cache()
        assert await effective_num_ctx(shared, "smart") == 2048, (
            "budget reader must return the delivered context"
        )
    finally:
        if isinstance(prior_num_ctx, int):
            try:
                await update_litellm_model(
                    "llm.smart_model",
                    model_id,
                    db_pool=SharedConnPool(contract_conn),
                    machine_id="ctx-contract-host",
                    num_ctx=prior_num_ctx,
                )
            except Exception:  # noqa: BLE001 — restore is best-effort
                pass


async def test_failed_delivery_keeps_previous_budget(contract_conn, ctx_client, monkeypatch):
    """Fail-closed: a delivery failure writes no rows, so the budget keeps the old value."""
    from fastapi import HTTPException

    import paper_ingestion.services.config_write as _config_write

    await _seed_system_row(contract_conn, "llm.smart_num_ctx", 8192)
    invalidate_effective_num_ctx_cache()

    async def _litellm_fail(**kwargs):  # noqa: ARG001
        raise HTTPException(status_code=400, detail="delivery-down")

    monkeypatch.setattr(_config_write, "_apply_litellm_runtime_update", _litellm_fail)

    resp = await ctx_client.put(
        f"/api/config/{_MACHINE_KEY}",
        json={"key": _MACHINE_KEY, "value": 4096},
    )
    assert resp.status_code == 400

    machine_row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL", _MACHINE_KEY
    )
    assert machine_row is None, "failed delivery must not commit the per-machine row"
    system_row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.smart_num_ctx' AND user_id IS NULL"
    )
    assert system_row is not None and system_row["value"] == 8192

    invalidate_effective_num_ctx_cache()
    assert await effective_num_ctx(SharedConnPool(contract_conn), "smart") == 8192, (
        "budget must never exceed the last successfully delivered context"
    )


async def test_num_ctx_validator_rejects_out_of_bounds_writes(ctx_client):
    """The write path 400s far-out values before any delivery is attempted."""
    resp = await ctx_client.put(
        f"/api/config/{_MACHINE_KEY}",
        json={"key": _MACHINE_KEY, "value": 2**40},
    )
    assert resp.status_code == 400, f"2**40 must be rejected, got {resp.status_code}"
    assert "num_ctx" in resp.json()["detail"]

    resp = await ctx_client.put(
        f"/api/config/{_MACHINE_KEY}",
        json={"key": _MACHINE_KEY, "value": 1024},
    )
    assert resp.status_code == 400, f"sub-minimum value must be rejected, got {resp.status_code}"
    assert "at least" in resp.json()["detail"]
