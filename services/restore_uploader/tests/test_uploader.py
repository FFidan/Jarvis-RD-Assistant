"""Boundary tests for the off-host DR upload ingress (stdlib http.client).

A threaded server on an ephemeral port serves a temp inbox + temp grant file.
Covers: happy path, path traversal, bad/expired/missing grant, disallowed name,
oversize (Content-Length + streamed), wrong method, and that the grant token
never reaches server stdout.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import uploader

_VALID = "jarvis_20260101_120000.sql.gz.enc"
_TOKEN = "grant-token-abcdef0123456789"


def _write_grant(trigger_dir, token: str = _TOKEN, ttl_s: int = 1800) -> None:
    (trigger_dir / ".upload_grant.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat(),
            }
        )
    )


@contextmanager
def _server(monkeypatch, tmp_path, max_gb: str = "20"):
    inbox = tmp_path / "inbox"
    trigger = tmp_path / "trigger"
    inbox.mkdir()
    trigger.mkdir()
    monkeypatch.setenv("RESTORE_INBOX_DIR", str(inbox))
    monkeypatch.setenv("BACKUP_TRIGGER_DIR", str(trigger))
    monkeypatch.setenv("UPLOAD_MAX_GB", max_gb)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), uploader.UploadHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1], inbox, trigger
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _put(port: int, path: str, body: bytes = b"payload", headers: dict | None = None) -> int:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("PUT", path, body=body, headers=headers or {})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status


def _grant_headers(token: str = _TOKEN) -> dict:
    return {"X-Upload-Grant": token}


def test_happy_path_stores_atomically_0600(monkeypatch, tmp_path):
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger)
        status = _put(port, f"/restore-upload/{_VALID}", b"archive-bytes", _grant_headers())
    assert status == 201
    stored = inbox / _VALID
    assert stored.read_bytes() == b"archive-bytes"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert not (inbox / f"{_VALID}.part").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/restore-upload/../secrets/x",
        "/restore-upload/%2e%2e%2fx",
        "/restore-upload/a/b",
        "/etc/passwd",
    ],
)
def test_traversal_and_offroute_rejected(monkeypatch, tmp_path, path):
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger)
        status = _put(port, path, b"x", _grant_headers())
    assert status in (400, 404)
    # Nothing was written anywhere under the inbox.
    assert list(inbox.iterdir()) == []


def test_bad_grant_rejected(monkeypatch, tmp_path):
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger)
        status = _put(port, f"/restore-upload/{_VALID}", b"x", _grant_headers("wrong-token"))
    assert status == 403
    assert list(inbox.iterdir()) == []


def test_expired_grant_rejected(monkeypatch, tmp_path):
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger, ttl_s=-10)
        status = _put(port, f"/restore-upload/{_VALID}", b"x", _grant_headers())
    assert status == 403
    assert list(inbox.iterdir()) == []


def test_missing_grant_file_rejected(monkeypatch, tmp_path):
    with _server(monkeypatch, tmp_path) as (port, inbox, _trigger):
        status = _put(port, f"/restore-upload/{_VALID}", b"x", _grant_headers())
    assert status == 403
    assert list(inbox.iterdir()) == []


def test_missing_grant_header_rejected(monkeypatch, tmp_path):
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger)
        status = _put(port, f"/restore-upload/{_VALID}", b"x", {})
    assert status == 401
    assert list(inbox.iterdir()) == []


@pytest.mark.parametrize("name", ["evil.sh", "jarvis_bad.sql", "operator_key.txt"])
def test_disallowed_name_rejected(monkeypatch, tmp_path, name):
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger)
        status = _put(port, f"/restore-upload/{name}", b"x", _grant_headers())
    assert status == 400
    assert list(inbox.iterdir()) == []


def test_oversize_content_length_rejected(monkeypatch, tmp_path):
    # cap ~1 KiB; declare a larger Content-Length.
    with _server(monkeypatch, tmp_path, max_gb="0.000001") as (port, inbox, trigger):
        _write_grant(trigger)
        status = _put(port, f"/restore-upload/{_VALID}", b"z" * 4096, _grant_headers())
    assert status == 413
    assert list(inbox.iterdir()) == []


def test_streamed_body_exceeding_cap_without_content_length_aborts(monkeypatch, tmp_path):
    with _server(monkeypatch, tmp_path, max_gb="0.000001") as (port, inbox, trigger):
        _write_grant(trigger)
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        req = (
            f"PUT /restore-upload/{_VALID} HTTP/1.1\r\n"
            "Host: x\r\n"
            f"X-Upload-Grant: {_TOKEN}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + b"z" * 8192
        sock.sendall(req)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        sock.close()
    assert b" 413 " in data.split(b"\r\n", 1)[0]
    assert not (inbox / f"{_VALID}.part").exists()
    assert list(inbox.iterdir()) == []


def test_truncated_body_under_content_length_rejected(monkeypatch, tmp_path):
    # Declare more bytes than we send, then half-close for a clean EOF: the short
    # .part must be rejected (400) and never committed to the final name.
    with _server(monkeypatch, tmp_path) as (port, inbox, trigger):
        _write_grant(trigger)
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        req = (
            f"PUT /restore-upload/{_VALID} HTTP/1.1\r\n"
            "Host: x\r\n"
            f"X-Upload-Grant: {_TOKEN}\r\n"
            "Content-Length: 50\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + b"short"
        sock.sendall(req)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        sock.close()
    assert b" 400 " in data.split(b"\r\n", 1)[0]
    assert not (inbox / _VALID).exists()
    assert not (inbox / f"{_VALID}.part").exists()
    assert list(inbox.iterdir()) == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_wrong_method_405(monkeypatch, tmp_path, method):
    with _server(monkeypatch, tmp_path) as (port, _inbox, trigger):
        _write_grant(trigger)
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, f"/restore-upload/{_VALID}", headers=_grant_headers())
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 405


def test_grant_token_never_logged(monkeypatch, tmp_path, capfd):
    with _server(monkeypatch, tmp_path) as (port, _inbox, trigger):
        _write_grant(trigger)
        _put(port, f"/restore-upload/{_VALID}", b"archive-bytes", _grant_headers())
    out = capfd.readouterr().out
    assert _TOKEN not in out
    assert "archive-bytes" not in out
