"""Platform API route modules."""

from platform_api.routers import (
    account,
    admin,
    audit_admin,
    auth,
    auth_passkeys,
    configuration,
    internal_auth,
    internal_telegram,
    setup,
    system,
    telegram,
)

__all__ = [
    "account",
    "admin",
    "audit_admin",
    "auth",
    "auth_passkeys",
    "configuration",
    "internal_auth",
    "internal_telegram",
    "setup",
    "system",
    "telegram",
]
