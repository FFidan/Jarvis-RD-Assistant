"""Tests for scripts/check-no-unsafe-resolver.py.

Exercises the linter logic directly so that CI catches regressions in the
alias-tracking and attribute-form Depends detection without needing to touch
live router files.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (it lives in scripts/, not on sys.path).
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-no-unsafe-resolver.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_no_unsafe_resolver", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_script()
_imports_unsafe = _mod._imports_unsafe
_depends_on_unsafe = _mod._depends_on_unsafe
_missing_resolver = _mod._missing_resolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(src: str) -> ast.Module:
    return ast.parse(src)


# ---------------------------------------------------------------------------
# _imports_unsafe
# ---------------------------------------------------------------------------


class TestImportsUnsafe:
    def test_direct_import_flagged(self) -> None:
        src = "from jarvis_common.auth import current_user_id_or_none"
        hits, aliases = _imports_unsafe(_parse(src))
        assert len(hits) == 1
        assert "import current_user_id_or_none" in hits[0][1]

    def test_aliased_import_flagged_and_alias_collected(self) -> None:
        src = "from jarvis_common.auth import current_user_id_or_none as uid"
        hits, aliases = _imports_unsafe(_parse(src))
        # The import line itself is still a violation.
        assert len(hits) == 1
        assert "import current_user_id_or_none" in hits[0][1]
        # The local alias must be tracked.
        assert "uid" in aliases

    def test_safe_import_not_flagged(self) -> None:
        src = "from jarvis_common.auth import current_user_id_strict"
        hits, aliases = _imports_unsafe(_parse(src))
        assert hits == []
        assert not aliases

    def test_multiple_unsafe_imports_flagged(self) -> None:
        src = (
            "from jarvis_common.auth import current_user_id_or_none\n"
            "from jarvis_common.auth import current_user_id_or_none as uid\n"
        )
        hits, aliases = _imports_unsafe(_parse(src))
        assert len(hits) == 2

    @pytest.mark.parametrize(
        "name",
        [
            "current_user_id_with_owner_override",
            "current_user_id_strict_with_owner_override",
            "get_current_user_id_or_bot",
        ],
    )
    def test_retired_owner_override_import_flagged(self, name: str) -> None:
        hits, _ = _imports_unsafe(_parse(f"from jarvis_common.auth import {name}"))
        assert hits == [(1, f"import {name}")]


# ---------------------------------------------------------------------------
# _depends_on_unsafe
# ---------------------------------------------------------------------------


class TestDependsOnUnsafe:
    def test_bare_name_flagged(self) -> None:
        src = "x = Depends(current_user_id_or_none)"
        hits = _depends_on_unsafe(_parse(src))
        assert len(hits) == 1
        assert "current_user_id_or_none" in hits[0][1]

    def test_aliased_name_flagged_when_alias_provided(self) -> None:
        """Depends(uid) must be caught when uid is a known alias."""
        src = "x = Depends(uid)"
        hits = _depends_on_unsafe(_parse(src), unsafe_aliases=frozenset({"uid"}))
        assert len(hits) == 1
        assert "uid" in hits[0][1]

    def test_attribute_form_flagged(self) -> None:
        """Depends(auth.current_user_id_or_none) must be caught."""
        src = "x = Depends(auth.current_user_id_or_none)"
        hits = _depends_on_unsafe(_parse(src))
        assert len(hits) == 1
        assert "current_user_id_or_none" in hits[0][1]

    @pytest.mark.parametrize(
        "src",
        [
            "x = Depends(uid)",
            "x = Depends(auth.current_user_id_strict)",
            "x = Depends(current_user_id_strict)",
        ],
        ids=("unknown-alias", "safe-attribute", "safe-name"),
    )
    def test_safe_dependency_not_flagged(self, src: str) -> None:
        """Unknown aliases and strict resolvers are not reported as unsafe."""
        assert _depends_on_unsafe(_parse(src)) == []


# ---------------------------------------------------------------------------
# Integration: aliased import + Depends(alias) in the same file
# ---------------------------------------------------------------------------


class TestAliasedImportPlusDependsIntegration:
    def test_alias_import_and_depends_both_flagged(self) -> None:
        """Full fixture: aliased import followed by Depends(alias).

        Both the import line and the Depends call must be flagged so that the
        combined violation list is non-empty.
        """
        src = (
            "from jarvis_common.auth import current_user_id_or_none as uid\n"
            "\n"
            "async def route(user_id: int = Depends(uid)):\n"
            "    pass\n"
        )
        tree = _parse(src)
        import_hits, unsafe_aliases = _imports_unsafe(tree)
        depends_hits = _depends_on_unsafe(tree, unsafe_aliases)
        all_hits = import_hits + depends_hits

        # Import line flagged.
        assert any("import current_user_id_or_none" in h[1] for h in import_hits)
        # Depends(uid) flagged.
        assert any("uid" in h[1] for h in depends_hits)
        # Combined list is non-empty → linter would exit 1.
        assert len(all_hits) >= 2

    def test_attribute_form_depends_flagged(self) -> None:
        """Depends(module.current_user_id_or_none) is caught via attribute form."""
        src = (
            "import jarvis_common.auth as auth\n"
            "\n"
            "async def route(user_id: int = Depends(auth.current_user_id_or_none)):\n"
            "    pass\n"
        )
        tree = _parse(src)
        # No ImportFrom → no import hits; but Depends hit via attribute form.
        _import_hits, unsafe_aliases = _imports_unsafe(tree)
        depends_hits = _depends_on_unsafe(tree, unsafe_aliases)
        assert any("current_user_id_or_none" in h[1] for h in depends_hits)


# ---------------------------------------------------------------------------
# _missing_resolver — the no-resolver-at-all class (root cause of B1/B2)
# ---------------------------------------------------------------------------


_REL = "services/x/x/routers/demo.py"


class TestMissingResolver:
    def test_route_with_no_resolver_flagged(self) -> None:
        """A handler with neither Depends(strict) nor a body call is flagged."""
        src = (
            "@router.get('/feedback-summary')\n"
            "async def feedback_summary(request: Request, db=Depends(get_db_pool)):\n"
            "    return {}\n"
        )
        hits = _missing_resolver(_parse(src), _REL)
        assert len(hits) == 1
        assert "GET /feedback-summary" in hits[0][1]
        assert "feedback_summary" in hits[0][1]

    @pytest.mark.parametrize(
        ("rel", "src"),
        [
            (
                _REL,
                "@router.get('/missing-foundational')\n"
                "async def mf(request: Request, "
                "user_id: int = Depends(current_user_id_strict)):\n"
                "    return []\n",
            ),
            (
                _REL,
                "@router.post('/scan')\n"
                "async def scan(request: Request):\n"
                "    user_id = await current_user_id_strict(request)\n"
                "    return user_id\n",
            ),
            (
                _REL,
                "@router.get('/x', dependencies=[Depends(require_admin)])\n"
                "async def x(request: Request):\n"
                "    return {}\n",
            ),
            (
                _REL,
                "router = APIRouter(dependencies=[Depends(current_user_id_strict)])\n"
                "@router.get('/a')\n"
                "async def a(request: Request):\n"
                "    return {}\n",
            ),
            (
                "services/paper_ingestion/paper_ingestion/routers/backups.py",
                "@router.post('/restore/acknowledge')\n"
                "async def acknowledge_restore(request: Request):\n"
                "    return None\n",
            ),
            (
                _REL,
                "@router.put('/{paper_id}/save')\n"
                "async def save(request: Request, "
                "user_id: int = Depends(get_current_user_id)):\n"
                "    return {}\n",
            ),
            (_REL, "async def helper(conn):\n    return 1\n"),
        ],
        ids=(
            "strict-dependency",
            "strict-body-call",
            "route-dependency",
            "router-dependency",
            "allowlisted-route",
            "bot-dependency",
            "non-route-function",
        ),
    )
    def test_resolved_or_exempt_handler_not_flagged(self, rel: str, src: str) -> None:
        """Handlers with a resolver or an explicit exemption are accepted."""
        assert _missing_resolver(_parse(src), rel) == []

    def test_unknown_resolver_name_still_flagged(self) -> None:
        """Only the named safe resolvers count — a look-alike does not.

        Guards the gate against being widened by accident: accepting any
        ``Depends(...)`` that resembles a resolver would green-light a route
        that establishes no identity at all.
        """
        src = (
            "@router.put('/{paper_id}/save')\n"
            "async def save(request: Request, "
            "user_id: int = Depends(get_current_user_id_or_anyone)):\n"
            "    return {}\n"
        )
        hits = _missing_resolver(_parse(src), _REL)
        assert len(hits) == 1
        assert "PUT /{paper_id}/save" in hits[0][1]

    def test_alias_mentioned_but_not_used_is_flagged(self) -> None:
        """Naming an authentication alias is not the same as depending on it.

        The alias resolves to a real resolver, but this handler never takes it
        as a parameter, so nothing authenticates the request.
        """
        src = (
            "type Principal = Annotated[str, Depends(authenticate_service_principal)]\n"
            "\n"
            "@router.post('/authorize')\n"
            "async def authorize(body: Request) -> Principal:\n"
            "    return {}\n"
        )
        hits = _missing_resolver(_parse(src), _REL)
        assert len(hits) == 1
        assert "POST /authorize" in hits[0][1]

    def test_alias_parameter_annotation_accepted(self) -> None:
        """A parameter annotated with the alias does inject the resolver."""
        src = (
            "type Principal = Annotated[str, Depends(authenticate_service_principal)]\n"
            "\n"
            "@router.post('/authorize')\n"
            "async def authorize(body: Request, principal: Principal):\n"
            "    return {}\n"
        )
        assert _missing_resolver(_parse(src), _REL) == []

    def test_alias_to_an_unsafe_resolver_is_not_accepted(self) -> None:
        """The alias stands in for its callable, so an unguarded one stays unguarded."""
        src = (
            "type Pool = Annotated[str, Depends(get_db_pool)]\n"
            "\n"
            "@router.post('/authorize')\n"
            "async def authorize(body: Request, db: Pool):\n"
            "    return {}\n"
        )
        assert len(_missing_resolver(_parse(src), _REL)) == 1

    def test_unguarded_sibling_router_is_flagged(self) -> None:
        """One guarded router must not clear the routes of a second, bare one."""
        src = (
            "router = APIRouter(dependencies=[Depends(current_user_id_strict)])\n"
            "public = APIRouter()\n"
            "\n"
            "@public.get('/summary')\n"
            "async def summary(request: Request):\n"
            "    return {}\n"
        )
        hits = _missing_resolver(_parse(src), _REL)
        assert len(hits) == 1
        assert "GET /summary" in hits[0][1]
