"""Shared database helpers for standalone scripts."""

from __future__ import annotations

import os
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import quote


def _checked_host(name: str, value: str) -> str:
    """Validate a database host while preserving bracketed IPv6 literals."""
    if not value:
        raise ValueError(f"{name} invalid host: empty")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{name} invalid host: whitespace")
    if any(ch in value for ch in "@/?#%"):
        raise ValueError(f"{name} invalid host: URI delimiter")
    if value.startswith("[") and value.endswith("]"):
        try:
            IPv6Address(value[1:-1])
        except ValueError as exc:
            raise ValueError(f"{name} invalid host: bracketed IPv6") from exc
        return value
    if "[" in value or "]" in value:
        raise ValueError(f"{name} invalid host: malformed brackets")
    if ":" in value:
        raise ValueError(f"{name} invalid host: unbracketed colon")
    if value.replace(".", "").isdigit():
        try:
            IPv4Address(value)
        except ValueError as exc:
            raise ValueError(f"{name} invalid host: IPv4") from exc
    return value


def _checked_port(name: str, value: str) -> str:
    """Validate a database TCP port."""
    if not value:
        raise ValueError(f"{name} invalid port: empty")
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} invalid port: ASCII decimal digits required")
    if not 1 <= int(value) <= 65535:
        raise ValueError(f"{name} invalid port: out of range")
    return value


def get_dsn() -> str:
    """Build a PostgreSQL DSN from environment variables.

    Prefers ``DATABASE_URL`` when set. Falls back to constructing a DSN from
    ``PGHOST`` / ``PGPORT`` / ``PGUSER`` / ``PGPASSWORD`` / ``PGDATABASE``
    with safe defaults (``localhost``, ``5432``, ``jarvis``, empty password).
    The user, password and database are percent-encoded, so a credential
    containing ``@``, ``/`` or ``#`` still parses as one component; the host
    and port are validated as separate component types.

    Returns
    -------
    str
        PostgreSQL connection string suitable for ``asyncpg.connect()``.

    Raises
    ------
    ValueError
        If ``PGHOST`` or ``PGPORT`` is not a valid constructed-DSN component.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = _checked_host("PGHOST", os.environ.get("PGHOST", "localhost"))
    port = _checked_port("PGPORT", os.environ.get("PGPORT", "5432"))
    user = quote(os.environ.get("PGUSER", "jarvis"), safe="")
    password = quote(os.environ.get("PGPASSWORD", ""), safe="")
    database = quote(os.environ.get("PGDATABASE", "jarvis"), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"
