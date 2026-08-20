"""Versioned route capabilities for signed internal identity assertions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

IdentityAudience = Literal["learning", "research"]
ServicePrincipal = Literal["learning", "research", "telegram"]

IDENTITY_CAPABILITY_VERSION: Final = 1

_AUDIENCES: Final = frozenset({"learning", "research"})
_PRINCIPALS: Final = frozenset({"learning", "research", "telegram"})
_READ_METHODS: Final = frozenset({"GET", "HEAD"})
# The complete set of backend routes served without a Platform-signed identity.
# Every other route outside ``/api`` must be named by a capability below, so a
# route added without one is refused rather than silently left unprotected.
_UNPROTECTED_PATHS: Final = frozenset(
    {
        "/docs",
        "/docs/oauth2-redirect",
        "/health",
        "/health/internal",
        "/health/live",
        "/openapi.json",
        "/redoc",
    }
)
_PATH_SEGMENT_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PLATFORM_CONFIG_WRITE_PATTERN: Final = re.compile(r"/internal/platform/config/[^/]+")
_PLATFORM_PROVIDER_CACHE_PATTERN: Final = re.compile(
    r"/internal/platform/providers/[^/]+/cache/invalidate"
)


@dataclass(frozen=True, slots=True)
class ServiceCapability:
    """One exact service-principal route capability.

    Parameters
    ----------
    principal : {"learning", "research", "telegram"}
        Calling service identity.
    audience : {"learning", "research"}
        Destination service.
    method : str
        Exact uppercase HTTP method.
    path_pattern : str
        Anchored regular expression matched against the request path.
    scope : str
        Minimum capability embedded in the signed assertion.
    may_name_subject : bool, default=False
        Whether the caller may name the acting user. Only background and
        cross-service commands qualify, because their subject is a stored owner
        Platform cannot re-derive from the caller. Every other capability acts
        for a subject Platform establishes itself, such as a Telegram pairing.
    """

    principal: ServicePrincipal
    audience: IdentityAudience
    method: str
    path_pattern: str
    scope: str
    may_name_subject: bool = False

    def matches(self, method: str, path: str) -> bool:
        """Return whether this capability authorizes an exact request binding.

        Parameters
        ----------
        method : str
            Validated uppercase HTTP method.
        path : str
            Validated absolute path without query or fragment text.

        Returns
        -------
        bool
            ``True`` only when both method and full path match.
        """
        return method == self.method and re.fullmatch(self.path_pattern, path) is not None


def _telegram_capability(
    audience: IdentityAudience,
    method: str,
    path_pattern: str,
    scope: str,
) -> ServiceCapability:
    return ServiceCapability("telegram", audience, method, path_pattern, scope)


# This product manifest intentionally enumerates Telegram's current command
# surface. A new bot command cannot gain backend authority merely by composing a
# URL: the route must be reviewed and added here with a negative contract test.
SERVICE_CAPABILITY_MANIFEST: Final[tuple[ServiceCapability, ...]] = (
    ServiceCapability(
        "learning",
        "research",
        "POST",
        r"/internal/domains/library",
        "research:library:write",
        may_name_subject=True,
    ),
    ServiceCapability(
        "research",
        "learning",
        "POST",
        r"/internal/domains/paper-read",
        "learning:domain:write",
        may_name_subject=True,
    ),
    ServiceCapability(
        "research",
        "learning",
        "POST",
        r"/internal/domains/paper-deleted",
        "learning:domain:write",
        may_name_subject=True,
    ),
    ServiceCapability(
        "research",
        "learning",
        "PUT",
        r"/internal/domains/projects/[^/]+/zotero-collection",
        "learning:domain:write",
        may_name_subject=True,
    ),
    ServiceCapability(
        "research",
        "learning",
        "PUT",
        r"/internal/domains/journal",
        "learning:domain:write",
        may_name_subject=True,
    ),
    _telegram_capability("learning", "GET", r"/api/projects", "learning:projects:read"),
    _telegram_capability("learning", "POST", r"/api/projects", "learning:projects:write"),
    _telegram_capability("learning", "GET", r"/api/projects/[^/]+", "learning:projects:read"),
    _telegram_capability("learning", "GET", r"/api/projects/[^/]+/tasks", "learning:tasks:read"),
    _telegram_capability(
        "learning", "GET", r"/api/projects/[^/]+/milestones", "learning:milestones:read"
    ),
    _telegram_capability("learning", "GET", r"/api/tasks", "learning:tasks:read"),
    _telegram_capability("learning", "PUT", r"/api/tasks/[^/]+", "learning:tasks:write"),
    _telegram_capability(
        "learning", "GET", r"/api/milestones/upcoming", "learning:milestones:read"
    ),
    _telegram_capability("learning", "GET", r"/api/stats", "learning:review:read"),
    _telegram_capability("learning", "GET", r"/api/review/next", "learning:review:read"),
    _telegram_capability("learning", "POST", r"/api/review/[^/]+", "learning:review:write"),
    _telegram_capability("learning", "GET", r"/api/executive/my-day", "learning:executive:read"),
    _telegram_capability("learning", "GET", r"/api/executive/focus/active", "learning:focus:read"),
    _telegram_capability(
        "learning",
        "GET",
        r"/api/executive/focus/telegram/pending",
        "learning:focus:read",
    ),
    _telegram_capability("learning", "POST", r"/api/executive/focus/start", "learning:focus:write"),
    _telegram_capability(
        "learning",
        "POST",
        r"/api/executive/focus/[^/]+/(?:pause|resume|complete|telegram-notified)",
        "learning:focus:write",
    ),
    _telegram_capability("learning", "GET", r"/internal/telegram/nudges", "learning:nudges:read"),
    _telegram_capability(
        "learning", "POST", r"/internal/telegram/nudges/[^/]+/ack", "learning:nudges:write"
    ),
    _telegram_capability("research", "GET", r"/api/papers/feed", "research:papers:read"),
    _telegram_capability("research", "GET", r"/api/papers/[^/]+", "research:papers:read"),
    _telegram_capability(
        "research",
        "PUT",
        r"/api/papers/[^/]+/(?:save|skip|reading|done|star|unstar|trash|trash_and_reject|restore)",
        "research:papers:write",
    ),
    _telegram_capability(
        "research", "POST", r"/api/papers/[^/]+/feedback", "research:papers:write"
    ),
    _telegram_capability("research", "POST", r"/api/authors/check", "research:authors:write"),
    _telegram_capability("research", "POST", r"/api/search", "research:search:write"),
    _telegram_capability("research", "GET", r"/api/pulse/today", "research:pulse:read"),
    _telegram_capability("research", "POST", r"/api/pulse/generate", "research:pulse:write"),
    _telegram_capability("research", "GET", r"/api/pulse/generate/[^/]+", "research:pulse:read"),
    _telegram_capability("research", "GET", r"/api/digest/weekly", "research:digest:read"),
)


def _internal_owner_scope(
    audience: IdentityAudience,
    method: str,
    path: str,
) -> tuple[str, ...] | None:
    """Return Platform-only owner scopes outside the service manifest."""
    bindings = (
        ("research", "PUT", _PLATFORM_CONFIG_WRITE_PATTERN, "research:config:write"),
        ("research", "POST", _PLATFORM_PROVIDER_CACHE_PATTERN, "research:providers:write"),
        (
            "research",
            "POST",
            re.compile(r"/internal/domains/erasure/[^/]+/(?:qdrant|research)"),
            "research:erasure:write",
        ),
        (
            "learning",
            "POST",
            re.compile(r"/internal/domains/erasure/[^/]+"),
            "learning:erasure:write",
        ),
    )
    for target, verb, pattern, scope in bindings:
        if audience == target and method == verb and pattern.fullmatch(path) is not None:
            return (scope,)
    return None


def required_identity_scopes(
    audience: IdentityAudience,
    method: str,
    path: str,
) -> tuple[str, ...] | None:
    """Return the minimum capability required for one backend request.

    Service-only routes use the exact scope from
    :data:`SERVICE_CAPABILITY_MANIFEST`. Public API routes use a stable
    destination, first-path-segment, and read/write capability. Every assertion
    additionally binds the exact method and path, so a token cannot be replayed
    against another route sharing the same capability family.

    Parameters
    ----------
    audience : {"learning", "research"}
        Exact destination domain.
    method : str
        Uppercase HTTP method.
    path : str
        Absolute request path without a query string.

    Returns
    -------
    tuple[str, ...] or None
        One required capability, or ``None`` for preflight and the named
        unprotected routes.

    Raises
    ------
    ValueError
        If the audience, method, or path is malformed, or if the path is
        neither unprotected nor covered by a capability.
    """
    _validate_binding(audience, method, path)
    if method == "OPTIONS" or path in _UNPROTECTED_PATHS:
        return None

    # Platform is the sole assertion signer. This exact non-public seam lets it
    # preserve Research-owned model and scheduler side effects while the public
    # configuration contract moves to Platform. Service principals cannot gain
    # this capability because it is deliberately absent from their manifest.
    internal_scope = _internal_owner_scope(audience, method, path)
    if internal_scope is not None:
        return internal_scope

    service_scopes = {
        capability.scope
        for capability in SERVICE_CAPABILITY_MANIFEST
        if capability.audience == audience and capability.matches(method, path)
    }
    if len(service_scopes) > 1:
        raise RuntimeError("service capability manifest assigns conflicting scopes")
    if service_scopes:
        return (service_scopes.pop(),)

    if path != "/api" and not path.startswith("/api/"):
        raise ValueError("request path is outside the identity capability boundary")
    segment = path.removeprefix("/api/").split("/", maxsplit=1)[0] or "api"
    if _PATH_SEGMENT_PATTERN.fullmatch(segment) is None:
        raise ValueError("request path has an invalid capability segment")
    access = "read" if method in _READ_METHODS else "write"
    return (f"{audience}:{segment}:{access}",)


def service_principal_scopes(
    principal: ServicePrincipal,
    audience: IdentityAudience,
    method: str,
    path: str,
) -> tuple[str, ...] | None:
    """Return a service principal's exact allowlisted route capability.

    Parameters
    ----------
    principal : {"learning", "research", "telegram"}
        Calling service identity.
    audience : {"learning", "research"}
        Exact destination service.
    method : str
        Uppercase HTTP method.
    path : str
        Absolute request path without query or fragment text.

    Returns
    -------
    tuple[str, ...] or None
        The one minimum scope for an allowlisted operation, or ``None`` when
        the operation is denied by default.

    Raises
    ------
    ValueError
        If the principal or request binding is malformed.
    """
    return _single_scope(_matching_capabilities(principal, audience, method, path))


def named_subject_scopes(
    principal: ServicePrincipal,
    audience: IdentityAudience,
    method: str,
    path: str,
) -> tuple[str, ...] | None:
    """Return the capability for a command that may name its own subject.

    Platform signs for whichever user the caller names here, so the capability
    must declare that its subject is a stored owner Platform cannot re-derive.
    Callers acting for a subject Platform establishes itself -- a paired
    Telegram chat, a browser session -- are denied and must use the boundary
    that resolves that subject.

    Parameters
    ----------
    principal : {"learning", "research", "telegram"}
        Calling service identity.
    audience : {"learning", "research"}
        Exact destination service.
    method : str
        Uppercase HTTP method.
    path : str
        Absolute request path without query or fragment text.

    Returns
    -------
    tuple[str, ...] or None
        The one minimum scope, or ``None`` when this caller may not name a
        subject for this route.

    Raises
    ------
    ValueError
        If the principal or request binding is malformed.
    """
    return _single_scope(
        tuple(
            capability
            for capability in _matching_capabilities(principal, audience, method, path)
            if capability.may_name_subject
        )
    )


def _matching_capabilities(
    principal: ServicePrincipal,
    audience: IdentityAudience,
    method: str,
    path: str,
) -> tuple[ServiceCapability, ...]:
    if principal not in _PRINCIPALS:
        raise ValueError("service principal is unsupported")
    _validate_binding(audience, method, path)
    return tuple(
        capability
        for capability in SERVICE_CAPABILITY_MANIFEST
        if capability.principal == principal
        and capability.audience == audience
        and capability.matches(method, path)
    )


def _single_scope(matches: tuple[ServiceCapability, ...]) -> tuple[str, ...] | None:
    if not matches:
        return None
    scopes = {capability.scope for capability in matches}
    if len(scopes) != 1:
        raise RuntimeError("service capability manifest assigns conflicting scopes")
    return (scopes.pop(),)


def _validate_binding(audience: str, method: str, path: str) -> None:
    if audience not in _AUDIENCES:
        raise ValueError("identity audience is unsupported")
    if not method or method != method.upper() or not method.isascii() or not method.isalpha():
        raise ValueError("request method must contain uppercase ASCII letters")
    if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
        raise ValueError("request path must be absolute and exclude query or fragment text")


__all__ = [
    "IDENTITY_CAPABILITY_VERSION",
    "SERVICE_CAPABILITY_MANIFEST",
    "IdentityAudience",
    "ServiceCapability",
    "ServicePrincipal",
    "named_subject_scopes",
    "required_identity_scopes",
    "service_principal_scopes",
]
