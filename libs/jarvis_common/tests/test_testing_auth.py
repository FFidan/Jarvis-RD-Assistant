"""Tests for ``jarvis_common.testing_auth`` (W5-03 / F-3 hoist).

Exercises ``_apply_default_authenticated_user`` context manager covering:
- module-level symbol monkeypatch of ``current_user_id_strict*`` resolvers
- ``app.dependency_overrides`` entry for ``current_user_id_strict_with_owner_override``
- cleanup-on-exit restoration of both
"""

from __future__ import annotations

import sys
import types
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from jarvis_common.testing_auth import _apply_default_authenticated_user


def _make_fake_routers_pkg(resolver_names: tuple[str, ...]) -> types.ModuleType:
    """Build a synthetic routers package with one submodule exposing each resolver name."""
    pkg = types.ModuleType("fake_routers_pkg")
    pkg_path = "/tmp/fake_routers_pkg_w5_03_test"
    pkg.__path__ = [pkg_path]
    sys.modules["fake_routers_pkg"] = pkg

    submodule = types.ModuleType("fake_routers_pkg.sub")
    for name in resolver_names:
        original = AsyncMock(return_value=42, name=f"original_{name}")
        setattr(submodule, name, original)
    sys.modules["fake_routers_pkg.sub"] = submodule

    return pkg


@pytest.fixture()
def fake_app() -> Any:
    """Minimal app stub with a ``dependency_overrides`` dict."""
    app = types.SimpleNamespace()
    app.dependency_overrides = {}
    return app


class _FakeFinder:
    """Stub MetaPathFinder satisfying ``pkgutil.ModuleInfo`` typing."""

    def find_spec(self, *_: Any, **__: Any) -> None:
        return None


def test_apply_default_authenticated_user_patches_resolvers(
    monkeypatch: pytest.MonkeyPatch, fake_app: Any
) -> None:
    """Context manager patches each strict resolver to AsyncMock(return_value=1)."""
    import pkgutil

    routers_pkg = _make_fake_routers_pkg(
        ("current_user_id_strict", "current_user_id_strict_with_owner_override")
    )
    monkeypatch.setattr(
        pkgutil,
        "iter_modules",
        lambda _paths: [pkgutil.ModuleInfo(cast(Any, _FakeFinder()), "sub", False)],
    )

    submodule = sys.modules["fake_routers_pkg.sub"]
    original_strict = submodule.current_user_id_strict
    original_owner = submodule.current_user_id_strict_with_owner_override

    with _apply_default_authenticated_user(fake_app, routers_pkg):
        assert submodule.current_user_id_strict is not original_strict
        assert submodule.current_user_id_strict_with_owner_override is not original_owner

    # post-exit: originals restored
    assert submodule.current_user_id_strict is original_strict
    assert submodule.current_user_id_strict_with_owner_override is original_owner


def test_apply_default_authenticated_user_sets_and_clears_dep_override(
    monkeypatch: pytest.MonkeyPatch, fake_app: Any
) -> None:
    """Context manager adds + removes the owner-override dep entry that returns 1."""
    import pkgutil

    from jarvis_common.auth import current_user_id_strict_with_owner_override

    routers_pkg = _make_fake_routers_pkg(())
    monkeypatch.setattr(
        pkgutil,
        "iter_modules",
        lambda _paths: [pkgutil.ModuleInfo(cast(Any, _FakeFinder()), "sub", False)],
    )

    assert current_user_id_strict_with_owner_override not in fake_app.dependency_overrides

    with _apply_default_authenticated_user(fake_app, routers_pkg):
        override = fake_app.dependency_overrides[current_user_id_strict_with_owner_override]
        assert override() == 1

    assert current_user_id_strict_with_owner_override not in fake_app.dependency_overrides


def test_apply_default_authenticated_user_preserves_existing_override(
    monkeypatch: pytest.MonkeyPatch, fake_app: Any
) -> None:
    """If override already present (test set it), context manager leaves it alone on exit."""
    import pkgutil

    from jarvis_common.auth import current_user_id_strict_with_owner_override

    routers_pkg = _make_fake_routers_pkg(())
    monkeypatch.setattr(
        pkgutil,
        "iter_modules",
        lambda _paths: [pkgutil.ModuleInfo(cast(Any, _FakeFinder()), "sub", False)],
    )

    sentinel = lambda: 99  # noqa: E731
    fake_app.dependency_overrides[current_user_id_strict_with_owner_override] = sentinel

    with _apply_default_authenticated_user(fake_app, routers_pkg):
        # Inner override NOT replaced
        assert fake_app.dependency_overrides[current_user_id_strict_with_owner_override] is sentinel

    assert fake_app.dependency_overrides[current_user_id_strict_with_owner_override] is sentinel


# ---------------------------------------------------------------------------
# W5-CF-COVERAGE-2: exception path — overrides restored on RuntimeError
# ---------------------------------------------------------------------------


def test_apply_default_authenticated_user_restores_overrides_on_exception(
    monkeypatch: pytest.MonkeyPatch, fake_app: Any
) -> None:
    """Context manager must restore dependency_overrides to pre-state when an exception escapes.

    Production path: _apply_default_authenticated_user (testing_auth.py:39-82)
    uses a try/finally block — the ``finally`` clause restores both
    module-level symbols and the ``app.dependency_overrides`` entry regardless
    of how the ``with`` block exits.
    """
    import pkgutil

    from jarvis_common.auth import current_user_id_strict_with_owner_override

    routers_pkg = _make_fake_routers_pkg(())
    monkeypatch.setattr(
        pkgutil,
        "iter_modules",
        lambda _paths: [pkgutil.ModuleInfo(cast(Any, _FakeFinder()), "sub", False)],
    )

    # Record the pre-state: override dict should be empty before entering.
    assert current_user_id_strict_with_owner_override not in fake_app.dependency_overrides
    pre_state_keys = set(fake_app.dependency_overrides.keys())

    with pytest.raises(RuntimeError, match="boom"):
        with _apply_default_authenticated_user(fake_app, routers_pkg):
            # Override is active inside the block.
            assert current_user_id_strict_with_owner_override in fake_app.dependency_overrides
            raise RuntimeError("boom")

    # Post-exit: dependency_overrides must be back to the pre-state.
    assert set(fake_app.dependency_overrides.keys()) == pre_state_keys
    assert current_user_id_strict_with_owner_override not in fake_app.dependency_overrides
