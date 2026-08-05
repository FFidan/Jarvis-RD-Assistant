from __future__ import annotations

from types import SimpleNamespace

import pytest
from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
    configure_contract_api_key,
    make_contract_client,
    patch_app_state,
    patch_dependency_overrides,
)


def test_configure_contract_api_key_restores_env_and_auth_cache(monkeypatch):
    from jarvis_common import auth

    monkeypatch.setenv("JARVIS_API_KEY", "original-api-key-value-that-is-long-enough")
    auth.refresh_api_key_cache()

    with configure_contract_api_key(monkeypatch) as key:
        assert key == DEFAULT_CONTRACT_API_KEY
        assert auth._CACHED_API_KEY == DEFAULT_CONTRACT_API_KEY

    assert auth._CACHED_API_KEY == "original-api-key-value-that-is-long-enough"


@pytest.mark.asyncio
async def test_make_contract_client_attaches_api_key_and_session_cookie():
    async def app(scope, receive, send):
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}
        body = (f"{headers.get('x-api-key')}|{headers.get('cookie')}").encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    async with make_contract_client(app, "session-cookie-value") as client:
        response = await client.get("/")

    assert response.text == f"{DEFAULT_CONTRACT_API_KEY}|jarvis_session=session-cookie-value"


def test_patch_app_state_restores_existing_and_missing_attrs():
    app = SimpleNamespace(state=SimpleNamespace(existing="before"))

    with patch_app_state(app, {"existing": "during", "created": "new"}):
        assert app.state.existing == "during"
        assert app.state.created == "new"

    assert app.state.existing == "before"
    assert not hasattr(app.state, "created")


def test_patch_app_state_can_keep_created_attrs_when_requested():
    app = SimpleNamespace(state=SimpleNamespace())

    with patch_app_state(app, {"created": "new"}, delete_missing=False):
        assert app.state.created == "new"

    assert app.state.created == "new"


def test_patch_app_state_remove_makes_attr_absent_and_restores():
    app = SimpleNamespace(state=SimpleNamespace(gone="before"))

    with patch_app_state(app, {}, remove=("gone", "never_there")):
        assert not hasattr(app.state, "gone")
        assert not hasattr(app.state, "never_there")
        app.state.never_there = "set-inside"

    assert app.state.gone == "before"
    assert not hasattr(app.state, "never_there")


def test_patch_app_state_rejects_name_both_removed_and_replaced():
    app = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(ValueError):
        with patch_app_state(app, {"attr": "x"}, remove=("attr",)):
            pass


def _pi_app_doubles():
    app = SimpleNamespace(state=SimpleNamespace(), dependency_overrides={})
    limiter = SimpleNamespace(enabled=True)

    def get_db_pool():  # pragma: no cover - never resolved in these tests
        return None

    return app, get_db_pool, limiter


def test_patch_pi_test_app_none_pool_leaves_db_pool_untouched():
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

    app, get_db_pool, limiter = _pi_app_doubles()

    with patch_pi_test_app(
        None,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(remove_owner_override=False),
    ):
        assert not hasattr(app.state, "db_pool")


def test_patch_pi_test_app_none_pool_rejects_db_dependency_override():
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

    app, get_db_pool, limiter = _pi_app_doubles()

    with pytest.raises(ValueError):
        with patch_pi_test_app(
            None,
            app=app,
            get_db_pool=get_db_pool,
            limiter=limiter,
            options=PITestAppOptions(remove_owner_override=False, override_db_dependency=True),
        ):
            pass


def test_patch_pi_test_app_state_absent_removes_and_restores():
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

    app, get_db_pool, limiter = _pi_app_doubles()
    app.state.qdrant_client = "leaked-from-elsewhere"
    pool = object()

    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(remove_owner_override=False, state_absent=("qdrant_client",)),
    ):
        assert not hasattr(app.state, "qdrant_client")
        assert app.state.db_pool is pool

    assert app.state.qdrant_client == "leaked-from-elsewhere"
    assert not hasattr(app.state, "db_pool")


def test_patch_dependency_overrides_restores_exact_keys():
    def dep_a():
        return "a"

    def dep_b():
        return "b"

    def dep_c():
        return "c"

    def original_b():
        return "original-b"

    def new_a():
        return "new-a"

    app = SimpleNamespace(dependency_overrides={dep_b: original_b})

    with patch_dependency_overrides(
        app,
        set_overrides={dep_a: new_a},
        remove_overrides={dep_b, dep_c},
    ):
        assert app.dependency_overrides == {dep_a: new_a}

    assert app.dependency_overrides == {dep_b: original_b}


def test_patch_dependency_overrides_rejects_ambiguous_same_key_set_and_remove():
    def dep():
        return None

    app = SimpleNamespace(dependency_overrides={})

    with pytest.raises(ValueError):
        with patch_dependency_overrides(
            app, set_overrides={dep: lambda: None}, remove_overrides={dep}
        ):
            pass
