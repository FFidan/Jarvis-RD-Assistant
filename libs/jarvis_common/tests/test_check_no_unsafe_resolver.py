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

    def test_both_unsafe_names_flagged(self) -> None:
        src = (
            "from jarvis_common.auth import current_user_id\n"
            "from jarvis_common.auth import current_user_id_or_none\n"
        )
        hits, aliases = _imports_unsafe(_parse(src))
        assert len(hits) == 2


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

    def test_aliased_name_not_flagged_without_alias_set(self) -> None:
        """Without the alias set, Depends(uid) should pass — uid is unknown."""
        src = "x = Depends(uid)"
        hits = _depends_on_unsafe(_parse(src))
        assert hits == []

    def test_attribute_form_flagged(self) -> None:
        """Depends(auth.current_user_id_or_none) must be caught."""
        src = "x = Depends(auth.current_user_id_or_none)"
        hits = _depends_on_unsafe(_parse(src))
        assert len(hits) == 1
        assert "current_user_id_or_none" in hits[0][1]

    def test_attribute_form_safe_name_not_flagged(self) -> None:
        src = "x = Depends(auth.current_user_id_strict)"
        hits = _depends_on_unsafe(_parse(src))
        assert hits == []

    def test_safe_depends_not_flagged(self) -> None:
        src = "x = Depends(current_user_id_strict)"
        hits = _depends_on_unsafe(_parse(src))
        assert hits == []


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
