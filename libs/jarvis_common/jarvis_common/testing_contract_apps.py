"""Shared contract-test app helpers.

These helpers keep service contract tests from open-coding the same API-key,
ASGI-client, app.state, and dependency-override restoration logic.  They are
deliberately small and service-agnostic; service conftests still own the actual
Paper Ingestion / Learning Engine app wiring.
"""

from __future__ import annotations

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
