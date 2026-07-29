"""Direct tests for the standalone script database helper."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

# scripts/ lives at the repo root, which is not in pytest's pythonpath.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts._db import get_dsn


def test_get_dsn_prefers_database_url(monkeypatch):
    """DATABASE_URL should take precedence over individual PG* variables."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://db-url")
    monkeypatch.setenv("PGHOST", "ignored-host")

    assert get_dsn() == "postgresql://db-url"


def test_get_dsn_builds_from_pg_components(monkeypatch):
    """get_dsn should compose the fallback DSN from the PG* environment."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", "5433")
    monkeypatch.setenv("PGUSER", "jarvis")
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setenv("PGDATABASE", "research")

    assert get_dsn() == "postgresql://jarvis:secret@db:5433/research"


def test_get_dsn_percent_encodes_each_credential(monkeypatch):
    """A credential holding reserved characters must stay one DSN component."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGUSER", "us@er")
    monkeypatch.setenv("PGPASSWORD", "p@ss/word#1")
    monkeypatch.setenv("PGDATABASE", "re/search")

    dsn = get_dsn()
    parts = urlsplit(dsn)

    assert unquote(parts.username or "") == "us@er"
    assert unquote(parts.password or "") == "p@ss/word#1"
    assert unquote(parts.path.lstrip("/")) == "re/search"
    # urlsplit divides the netloc on the LAST "@", while asyncpg's own parser
    # uses the FIRST, so the round trip above passes even on a raw user. Pin
    # the invariants that parser actually depends on: exactly one separator,
    # one path segment, and nothing siphoned off into a fragment.
    assert dsn.count("@") == 1
    assert parts.path.count("/") == 1
    assert parts.fragment == ""


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PGHOST", "db/svc"),
        ("PGHOST", "evil@host"),
        ("PGHOST", "host with space"),
        ("PGPORT", "5432/x"),
    ],
)
def test_get_dsn_rejects_a_host_or_port_that_would_resplit_the_dsn(monkeypatch, variable, value):
    """A host or port carrying a netloc delimiter must fail loudly.

    Percent-encoding cannot rescue these: encoding a hostname changes which host
    is resolved. Left unchecked, ``PGHOST=db/svc`` ends the netloc at the slash,
    so the connection silently reaches host ``db`` on the default port with the
    database name swallowed into the path.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=variable):
        get_dsn()


def test_get_dsn_accepts_a_bracketed_ipv6_host(monkeypatch):
    """The guard must not reject the reserved characters IPv6 literals require."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "[::1]")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGUSER", "jarvis")
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setenv("PGDATABASE", "jarvis")

    assert get_dsn() == "postgresql://jarvis:secret@[::1]:5432/jarvis"


def test_get_dsn_uses_defaults_for_missing_pg_components(monkeypatch):
    """Missing PG* values should fall back to the script defaults."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)

    assert get_dsn() == "postgresql://jarvis:@localhost:5432/jarvis"


@pytest.mark.parametrize(
    "host",
    ["db", "postgres-primary", "host.docker.internal", "192.168.1.10", "[::1]", "[2001:db8::1]"],
)
def test_get_dsn_accepts_valid_hosts(monkeypatch, host):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", host)
    monkeypatch.setenv("PGPORT", "5432")

    assert f"@{host}:5432/" in get_dsn()


@pytest.mark.parametrize(
    "host",
    [
        "",
        "db:5432",
        "[::1",
        "::1",
        "[not-ipv6]",
        "999.1.1.1",
        "db/svc",
        "evil@host",
        "host with space",
        "db?query",
        "db#fragment",
        "db]",
        "%2Ftmp",
        "%2ftmp",
        "%40host",
        "%3Fquery",
        "%23fragment",
    ],
)
def test_get_dsn_rejects_invalid_hosts_while_constructing_dsn(monkeypatch, host):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", host)
    monkeypatch.setenv("PGPORT", "5432")

    with pytest.raises(ValueError, match="PGHOST.*invalid host"):
        get_dsn()


@pytest.mark.parametrize("port", ["1", "5432", "65535"])
def test_get_dsn_accepts_valid_ports(monkeypatch, port):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", port)

    assert f":{port}/" in get_dsn()


@pytest.mark.parametrize(
    "port",
    ["", "5432/path", "5432:5433", "0", "65536", "+5432", "-1", "５４３２", " 5432", "port"],
)
def test_get_dsn_rejects_invalid_ports_while_constructing_dsn(monkeypatch, port):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", port)

    with pytest.raises(ValueError, match="PGPORT.*invalid port"):
        get_dsn()
