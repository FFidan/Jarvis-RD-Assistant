"""Shared contract-test app helpers.

These helpers keep service contract tests from open-coding the same API-key,
ASGI-client, app.state, and dependency-override restoration logic.  They are
deliberately small and service-agnostic; service conftests still own the actual
Paper Ingestion / Learning Engine app wiring.
"""

from __future__ import annotations

__all__ = [
    # pre-existing
    "configure_contract_api_key",
    "make_contract_client",
    "patch_app_state",
    "patch_dependency_overrides",
    # cluster 10 (moved from testing.py in 2026-05-24 polish wave)
    "_make_pi_contract_app_with_litellm_sidecar",
    "_make_le_contract_app_with_litellm_sidecar",
]

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import httpx

DEFAULT_CONTRACT_API_KEY = "contract-test-key-do-not-use-in-production-2026"

_MISSING = object()


def _refresh_auth_settings() -> None:
    from jarvis_common import auth
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()
    auth.refresh_api_key_cache()


@contextmanager
def configure_contract_api_key(
    monkeypatch: Any, key: str = DEFAULT_CONTRACT_API_KEY
) -> Iterator[str]:
    """Temporarily configure the process API key and refresh auth caches."""
    original = os.environ.get("JARVIS_API_KEY", _MISSING)
    monkeypatch.setenv("JARVIS_API_KEY", key)
    _refresh_auth_settings()
    try:
        yield key
    finally:
        if original is _MISSING:
            monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        else:
            monkeypatch.setenv("JARVIS_API_KEY", str(original))
        _refresh_auth_settings()


def make_contract_client(
    app: Any,
    session_cookie: str | None,
    *,
    api_key: str = DEFAULT_CONTRACT_API_KEY,
    base_url: str = "http://test",
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """Return an ASGI client with the repo's standard contract auth headers."""
    cookies = {"jarvis_session": session_cookie} if session_cookie is not None else None
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        headers={"X-API-Key": api_key},
        cookies=cookies,
        follow_redirects=follow_redirects,
    )


@contextmanager
def patch_app_state(
    app: Any,
    replacements: Mapping[str, Any],
    *,
    delete_missing: bool = True,
) -> Iterator[None]:
    """Patch ``app.state`` attributes and restore the exact previous state."""
    original = {
        name: getattr(app.state, name) if hasattr(app.state, name) else _MISSING
        for name in replacements
    }
    for name, value in replacements.items():
        setattr(app.state, name, value)
    try:
        yield
    finally:
        for name, value in original.items():
            if value is _MISSING:
                if delete_missing and hasattr(app.state, name):
                    delattr(app.state, name)
            else:
                setattr(app.state, name, value)


@contextmanager
def patch_dependency_overrides(
    app: Any,
    *,
    set_overrides: Mapping[Any, Any] | None = None,
    remove_overrides: set[Any] | frozenset[Any] | tuple[Any, ...] | list[Any] | None = None,
) -> Iterator[None]:
    """Patch FastAPI dependency overrides without clearing unrelated entries."""
    to_set = dict(set_overrides or {})
    to_remove = set(remove_overrides or ())
    overlap = to_remove.intersection(to_set)
    if overlap:
        raise ValueError("dependency override keys cannot be both removed and set")

    overrides = app.dependency_overrides
    touched = set(to_set).union(to_remove)
    original = {key: overrides.get(key, _MISSING) for key in touched}

    for key in to_remove:
        overrides.pop(key, None)
    overrides.update(to_set)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is _MISSING:
                overrides.pop(key, None)
            else:
                overrides[key] = value


# === Cluster 10: LiteLLM contract-app builders ===
# (moved from testing.py in the 2026-05-24 polish-wave decomposition)


def _make_pi_contract_app_with_litellm_sidecar():
    """Return a session-scoped fixture that wires the PI app to a FauxLiteLLMServer.

    Yields ``(app, faux_server)`` so contract tests can both drive the HTTP API
    and script LLM responses without touching real LiteLLM infrastructure.

    Depends on ``_pi_app_with_pool`` (defined in
    ``services/paper_ingestion/tests/conftest.py``) which wires the real DB
    connection.  The faux server overrides ``LITELLM_BASE_URL`` so every call
    to ``get_litellm_config()`` inside the app routes to the sidecar.

    Future extension: a parallel ``_make_le_contract_app_with_litellm_sidecar``
    can be added here with the same pattern once the LE app fixture is stable.
    See docs/contracts/07-testing.md for the full rollout plan.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="function", loop_scope="session")
    async def pi_contract_app_with_litellm_sidecar(_pi_app_with_pool, monkeypatch):
        """Yield (app, faux_litellm_server) with LITELLM_BASE_URL pointed at the sidecar.

        Usage in a contract test::

            async def test_rag_answer(pi_contract_app_with_litellm_sidecar, contract_two_users):
                app, faux = pi_contract_app_with_litellm_sidecar
                faux.add_pydantic_response("smart", MyAnswer(text="hi", citations=[]))
                async with make_contract_client(app, contract_two_users.cookie_a) as c:
                    resp = await c.post("/api/ask", json={"question": "test"})
                assert resp.status_code == 200
        """
        import instructor
        import openai

        from jarvis_common.testing_sidecars import FauxLiteLLMServer

        async with FauxLiteLLMServer() as srv:
            monkeypatch.setenv("LITELLM_BASE_URL", srv.url)
            # Build an Instructor-patched AsyncOpenAI pointed at the faux server
            # (mirrors app_factory.py:401 construction so Instructor mode is identical)
            oc = instructor.from_openai(
                openai.AsyncOpenAI(base_url=f"{srv.url}/v1", api_key="dummy"),
                mode=instructor.Mode.JSON,
            )
            with patch_app_state(_pi_app_with_pool, {"openai_client": oc}):
                yield _pi_app_with_pool, srv

    return pi_contract_app_with_litellm_sidecar


def _make_le_contract_app_with_litellm_sidecar(set_services_fn, reset_services_fn):
    """Same shape as _make_pi_contract_app_with_litellm_sidecar but for LE app.

    Yields ``(app, faux_server)`` so LE contract tests can script LLM responses
    via the FauxLiteLLMServer without touching real LiteLLM infrastructure.

    Depends on ``_le_app`` (defined in
    ``services/learning_engine/tests/conftest.py``) which wires the real DB
    connection and standard LE collaborators.  The faux server overrides
    ``LITELLM_BASE_URL`` so every call to ``get_litellm_config()`` inside the
    app routes to the sidecar.

    ``set_services_fn`` and ``reset_services_fn`` are callables injected by the
    service conftest (from ``learning_engine._state``) so that this library
    function does not import from any service package.
    """
    import pytest_asyncio

    @pytest_asyncio.fixture(scope="function", loop_scope="session")
    async def le_contract_app_with_litellm_sidecar(_le_app, monkeypatch):
        """Yield (app, faux_litellm_server) with LITELLM_BASE_URL pointed at the sidecar.

        Usage in a contract test::

            async def test_card_gen(le_contract_app_with_litellm_sidecar, contract_two_users):
                app, faux = le_contract_app_with_litellm_sidecar
                faux.add_pydantic_response("smart", CardGenerationOutput(...))
                async with make_contract_client(app, contract_two_users.cookie_a) as c:
                    resp = await c.post("/api/generate", json={...})
                assert resp.status_code == 202
        """
        import instructor
        import openai

        from jarvis_common.testing_sidecars import FauxLiteLLMServer

        _le_set_services = set_services_fn
        _le_reset_services = reset_services_fn

        async with FauxLiteLLMServer() as srv:
            monkeypatch.setenv("LITELLM_BASE_URL", srv.url)
            oc = instructor.from_openai(
                openai.AsyncOpenAI(base_url=f"{srv.url}/v1", api_key="dummy"),
                mode=instructor.Mode.JSON,
            )
            # Patch both app.state.openai_client (for router deps) and
            # learning_engine._state.svc.openai_client (for generate_cards_core
            # which calls get_services().openai_client directly).
            _le_set_services(openai_client=oc)
            try:
                with patch_app_state(_le_app, {"openai_client": oc}):
                    yield _le_app, srv
            finally:
                _le_reset_services()

    return le_contract_app_with_litellm_sidecar
