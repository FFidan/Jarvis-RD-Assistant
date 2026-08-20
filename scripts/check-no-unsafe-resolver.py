#!/usr/bin/env python3
"""Auth guard: enforce a safe user-id resolver on every router endpoint.

Two failure classes are caught:

1. *Permissive resolver* — ``current_user_id_or_none``
   resolve to ``None`` for API-key-only callers, letting an ops credential
   fall through as a permissionless shared user. User-data routes must use
   ``current_user_id_strict`` (hard 401) instead.

2. *No resolver at all* — an endpoint that never establishes a caller
   identity (no ``Depends(current_user_id_strict)`` parameter, no
   ``dependencies=[Depends(...)]`` on the route/router, no direct
   ``await current_user_id_strict(request)`` call in the body). This is how
   the analytics cross-user leaks (B1/B2) slipped past the old linter, which
   only looked for *uses* of the permissive resolver.

A route is considered guarded when a name in :data:`SAFE_NAMES` is *invoked*:
as the argument of a ``Depends(...)`` in a parameter default or in a route- or
router-level ``dependencies=[...]`` list, through a parameter annotated with a
``type X = Annotated[..., Depends(<safe>)]`` alias, or as a direct call in the
handler body. A bare mention — a return annotation, a local variable, a string
— is not proof of authentication and never clears a handler. Genuinely
public/ops routes are listed either by file (:data:`ALLOWLIST`) or by exact
path (:data:`ROUTE_ALLOWLIST`).

Pure stdlib (``ast`` + ``pathlib``); exits non-zero on any violation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Forbidden resolver names (the permissive, None-returning ones).
UNSAFE_NAMES = frozenset(
    {
        "current_user_id_or_none",
        "current_user_id_with_owner_override",
        "current_user_id_strict_with_owner_override",
        "get_current_user_id_or_bot",
    }
)

# Resolvers that establish a real, non-None caller identity (or hard 401/403).
# Callables only: a type alias belongs here only through the callable its
# ``Depends(...)`` names, which _safe_type_aliases resolves.
SAFE_NAMES = frozenset(
    {
        "current_user_id_strict",
        "get_current_user_id",
        "require_admin",
        "require_admin_or_api_key",
        "require_telegram_principal",
        "require_unconfigured_or_admin",
        "authenticate_service_principal",
    }
)

_ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete"})

# Router files that legitimately serve ops/public traffic (no per-user data
# to protect) and may keep the permissive resolvers. Paths are relative to the
# repository root. Add an exception only when the route is demonstrably safe.
ALLOWLIST = frozenset(
    {
        "services/paper_ingestion/paper_ingestion/routers/system.py",
    }
)

# Per-route allowlist for the no-resolver check: routes that are genuinely
# public, operational, or use shared catalog data without a per-user dimension.
# Key is ``"<rel_path>::<METHOD> <route_path>"``; value is a one-line
# justification. Per-user endpoints belong in caller-identity checks, not here.
ROUTE_ALLOWLIST: dict[str, str] = {
    # Public auth flow — establishes identity, cannot require one first.
    "services/platform_api/platform_api/routers/auth.py::POST /request-link": "public: starts magic-link auth",  # noqa: E501
    "services/platform_api/platform_api/routers/auth.py::POST /verify": "public: completes magic-link auth",  # noqa: E501
    "services/platform_api/platform_api/routers/auth.py::POST /logout": "session teardown, no user data read",  # noqa: E501
    "services/platform_api/platform_api/routers/auth.py::POST /api-key-session": "public: validates an API key and creates a session",  # noqa: E501
    # Passkey sign-in — the same class as the magic-link flow above: a login
    # ceremony cannot require the session it exists to create. /login/begin is
    # discoverable (username-less), so it takes no identifier and cannot enumerate
    # users; both ceremonies require user verification and are origin-matched.
    "services/platform_api/platform_api/routers/auth_passkeys.py::POST /login/begin": "public: starts passkey auth (username-less, no user enumeration)",  # noqa: E501
    "services/platform_api/platform_api/routers/auth_passkeys.py::POST /login/finish": "public: completes passkey auth",  # noqa: E501
    "services/platform_api/platform_api/routers/auth_passkeys.py::POST /capability": "public: reports whether this origin can run passkey ceremonies; no per-user data (POST so the browser attaches Origin on the same-origin probe)",  # noqa: E501
    # Platform-owned bootstrap and gateway-auth routes use purpose-built guards
    # rather than caller-ID dependencies.
    "services/platform_api/platform_api/routers/internal_auth.py::GET /authorize": "internal: trusted gateway peer plus session or operations-key authorization",  # noqa: E501
    "services/platform_api/platform_api/routers/setup.py::GET /status": "public: fail-closed setup-state probe required before authentication",  # noqa: E501
    "services/platform_api/platform_api/routers/setup.py::POST /admin": "bootstrap: setup-token-gated first-admin creation under a database lock",  # noqa: E501
    # Restore status requires an admin session, operations key, or restore token.
    # It resolves no user ID, so the caller-identity scan cannot recognize that
    # authentication. The database-independent poll remains available during a
    # database swap and exposes progress only, never archive contents.
    "services/paper_ingestion/paper_ingestion/routers/backups.py::GET /restore/status": "authenticated by restore_status_auth without database access, so it survives the restore swap",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/backups.py::POST /restore/acknowledge": "authenticated by restore_acknowledgement_auth using the one-time restore token or configured-owner session",  # noqa: E501
    # Shared global catalog tables (topics, extraction templates, sources) —
    # not per-user data.
    "services/paper_ingestion/paper_ingestion/routers/extractions.py::GET /extraction-templates": "shared extraction-template catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/extractions.py::POST /extraction-templates": "shared extraction-template catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/extractions.py::PUT /extraction-templates/{template_id}": "shared extraction-template catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/extractions.py::DELETE /extraction-templates/{template_id}": "shared extraction-template catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/settings_sources.py::GET /sources": "shared source-plugin registry",  # noqa: E501
    # Internal domain and job commands are authenticated by the signed-identity
    # middleware, then bind the verified principal, subject, path and request ID
    # inside each handler. They deliberately do not resolve browser sessions.
    "services/learning_engine/learning_engine/routers/internal_domains.py::POST /paper-read": "internal: exact signed Research command with subject and request binding",  # noqa: E501
    "services/learning_engine/learning_engine/routers/internal_domains.py::POST /paper-deleted": "internal: exact signed Research command with subject and request binding",  # noqa: E501
    "services/learning_engine/learning_engine/routers/internal_domains.py::PUT /projects/{project_id}/zotero-collection": "internal: exact signed Research command with subject and request binding",  # noqa: E501
    "services/learning_engine/learning_engine/routers/internal_domains.py::PUT /journal": "internal: exact signed Research command with subject and request binding",  # noqa: E501
    "services/learning_engine/learning_engine/routers/internal_domains.py::POST /erasure/{request_id}": "internal: exact signed Platform erasure command with subject and request binding",  # noqa: E501
    "services/learning_engine/learning_engine/routers/jobs.py::POST /dispatch": "internal: exact signed Platform job dispatch bound to the verified user",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/internal_domains.py::POST /erasure/{request_id}/qdrant": "internal: exact signed Platform erasure command with subject and request binding",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/internal_domains.py::POST /erasure/{request_id}/research": "internal: exact signed Platform erasure command with subject and request binding",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/internal_domains.py::POST /library": "internal: exact signed Learning command with subject and request binding",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/jobs.py::POST /dispatch": "internal: exact signed Platform job dispatch bound to the verified user",  # noqa: E501
    # Shared global topics catalog (`topics` has no user_id column;
    # per-user subscriptions live on the *subscription* routes, which do
    # resolve identity). CRUD here mutates the shared catalog only.
    "services/paper_ingestion/paper_ingestion/routers/topics.py::GET ": "shared topics catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/topics.py::POST ": "shared topics catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/topics.py::PUT /{topic_id}": "shared topics catalog",  # noqa: E501
    "services/paper_ingestion/paper_ingestion/routers/topics.py::DELETE /{topic_id}": "shared topics catalog",  # noqa: E501
}


def _route_meta(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, str, str]]:
    """Return ``(router, METHOD, path)`` for every ``@router.<verb>(...)`` decorator."""
    out: list[tuple[str, str, str]] = []
    for dec in func.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        if dec.func.attr not in _ROUTE_DECORATORS:
            continue
        router = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ""
        path = ""
        if dec.args and isinstance(dec.args[0], ast.Constant):
            path = str(dec.args[0].value)
        out.append((router, dec.func.attr.upper(), path))
    return out


def _names_in(node: ast.AST) -> set[str]:
    """Collect every ``Name``/``Attribute`` identifier referenced under *node*."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def _depended_names(node: ast.AST) -> set[str]:
    """Collect the callable each ``Depends(...)`` under *node* actually injects."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)):
            continue
        if sub.func.id != "Depends" or not sub.args:
            continue
        arg = sub.args[0]
        if isinstance(arg, ast.Name):
            found.add(arg.id)
        elif isinstance(arg, ast.Attribute):
            found.add(arg.attr)
    return found


def _called_names(node: ast.AST) -> set[str]:
    """Collect every callable invoked directly under *node*.

    This is the ``user_id = await current_user_id_strict(request)`` form, which
    predates the dependency idiom and is still used outside route handlers.
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name):
            found.add(sub.func.id)
        elif isinstance(sub.func, ast.Attribute):
            found.add(sub.func.attr)
    return found


def _safe_type_aliases(tree: ast.Module) -> frozenset[str]:
    """Return ``type X = Annotated[..., Depends(<safe>)]`` alias names.

    A handler annotated with such an alias is authenticated by the callable the
    alias injects, so the alias stands in for that callable — and only for as
    long as it keeps naming one. The alias name itself is never a safe name:
    mentioning it without using it as a parameter annotation proves nothing.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            if _depended_names(node.value) & SAFE_NAMES:
                aliases.add(node.name.id)
    return frozenset(aliases)


def _safe_routers(tree: ast.Module) -> frozenset[str]:
    """Return the names bound to ``APIRouter(dependencies=[Depends(<safe>)])``.

    Binding the guard to the router *name* keeps a second, unguarded router in
    the same module from inheriting the first one's dependencies.
    """
    routers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            continue
        if call.func.id != "APIRouter":
            continue
        if not any(
            kw.arg == "dependencies" and _depended_names(kw.value) & SAFE_NAMES
            for kw in call.keywords
        ):
            continue
        routers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return frozenset(routers)


def _handler_is_guarded(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    safe_aliases: frozenset[str],
) -> bool:
    """Whether *func* establishes a caller identity through one of the safe forms."""
    if _depended_names(func) & SAFE_NAMES:
        return True
    if _called_names(func) & SAFE_NAMES:
        return True
    args = func.args
    annotations = (
        arg.annotation
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
        if arg.annotation is not None
    )
    return any(_names_in(annotation) & safe_aliases for annotation in annotations)


def _imports_unsafe(tree: ast.Module) -> tuple[list[tuple[int, str]], frozenset[str]]:
    """Return import violations and local alias names for unsafe resolvers."""
    hits: list[tuple[int, str]] = []
    unsafe_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in UNSAFE_NAMES:
                    hits.append((node.lineno, f"import {alias.name}"))
                    local_name = alias.asname if alias.asname else alias.name
                    unsafe_aliases.add(local_name)
    return hits, frozenset(unsafe_aliases)


def _depends_on_unsafe(
    tree: ast.Module,
    unsafe_aliases: frozenset[str] = frozenset(),
) -> list[tuple[int, str]]:
    """Return (lineno, name) for every ``Depends(<unsafe>)`` call."""
    all_unsafe_names = UNSAFE_NAMES | unsafe_aliases
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Depends" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name) and arg.id in all_unsafe_names:
            hits.append((node.lineno, f"Depends({arg.id})"))
        elif isinstance(arg, ast.Attribute) and arg.attr in UNSAFE_NAMES:
            hits.append((node.lineno, f"Depends(…{arg.attr})"))
    return hits


def _missing_resolver(
    tree: ast.Module,
    rel: str,
) -> list[tuple[int, str]]:
    """Return violations for route handlers that establish no caller identity.

    A handler is guarded when it *invokes* a safe resolver — through a
    ``Depends(<safe>)`` in its decorators or signature, through a parameter
    annotated with a safe ``Annotated[..., Depends(<safe>)]`` alias, or through
    a direct call in its body — when the router it is registered on carries a
    safe dependency, or when the exact route is in :data:`ROUTE_ALLOWLIST`.
    """
    safe_aliases = _safe_type_aliases(tree)
    safe_routers = _safe_routers(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        routes = _route_meta(node)
        if not routes:
            continue
        if _handler_is_guarded(node, safe_aliases):
            continue
        for router, method, path in routes:
            if router in safe_routers:
                continue
            if f"{rel}::{method} {path}" in ROUTE_ALLOWLIST:
                continue
            hits.append(
                (node.lineno, f"{method} {path or '/'} ({node.name}): no caller-identity resolver")
            )
    return hits


def main() -> int:
    """Scan all router files for unsafe user-id resolver usage and missing resolvers.

    Returns
    -------
    int
        0 if all routers are clean, 1 if any violations are found.
    """
    routers = sorted(_REPO_ROOT.glob("services/*/*/routers/*.py"))
    failed = False
    for path in routers:
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            print(f"{rel}: could not parse ({exc})", file=sys.stderr)
            failed = True
            continue
        import_hits, unsafe_aliases = _imports_unsafe(tree)
        violations = import_hits + _depends_on_unsafe(tree, unsafe_aliases)
        if violations:
            failed = True
            print(f"{rel}: uses permissive user-id resolver (use current_user_id_strict):")
            for lineno, what in sorted(violations):
                print(f"  {rel}:{lineno}: {what}")

        missing = _missing_resolver(tree, rel)
        if missing:
            failed = True
            print(f"{rel}: route handler(s) with no caller-identity resolver:")
            for lineno, what in sorted(missing):
                print(f"  {rel}:{lineno}: {what}")

    if failed:
        print(
            "\nWS-AUTH: the files above must use current_user_id_strict (or a "
            "require_admin* guard), be added to ALLOWLIST if the whole file is "
            "ops/public, or have the specific public/global-catalog route added "
            "to ROUTE_ALLOWLIST with a justification.",
            file=sys.stderr,
        )
        return 1
    print("check-no-unsafe-resolver: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
