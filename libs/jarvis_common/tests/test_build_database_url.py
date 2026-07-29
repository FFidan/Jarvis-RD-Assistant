"""Unit tests for ``app_factory.build_database_url`` DSN construction.

Covers percent-encoding of the credentials interpolated into the DSN: a
user, password and database name containing characters reserved in URI
syntax (``@ : / # %``) must not break DSN parsing.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from jarvis_common import app_factory


def test_build_database_url_percent_encodes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User, password and db with URI-reserved characters round-trip through the DSN."""
    user = "us@er"
    password = "p@ss/w0rd:x#1"
    db = "db/name#1"
    monkeypatch.setenv("POSTGRES_USER", user)
    monkeypatch.setenv("POSTGRES_DB", db)
    secret_file = tmp_path / "postgres_password"
    secret_file.write_text(password)
    monkeypatch.setattr(app_factory, "POSTGRES_PASSWORD_SECRET_PATH", str(secret_file))

    url = app_factory.build_database_url()
    parts = urlsplit(url)

    # A raw "@" in the user must not survive: consumers that split userinfo
    # from host on the first "@" (unlike urlsplit, which uses the last)
    # would otherwise misparse the host. Exactly one literal "@" may remain
    # -- the userinfo/host separator.
    assert url.count("@") == 1
    assert unquote(parts.username or "") == user
    assert unquote(parts.password or "") == password
    # The path of a correctly-encoded DSN is exactly one "/" followed by the
    # encoded database name, so no reserved character in the name can reach the
    # parser. Round-tripping alone would not catch a raw "/" -- urlsplit keeps
    # it in the path and lstrip("/") reproduces the name unchanged -- while a
    # raw "#" or "?" would silently move part of the name into the fragment or
    # query rather than the database.
    assert parts.path.count("/") == 1
    assert parts.fragment == ""
    assert parts.query == ""
    assert unquote(parts.path.lstrip("/")) == db
    assert parts.hostname == "postgres"
