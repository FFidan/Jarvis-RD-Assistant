"""JARVIS off-host disaster-recovery upload ingress (stdlib only, no deps).

TRUST BOUNDARY — this is why a *separate* container exists:
    During a browser-driven off-host restore the operator uploads the ENCRYPTED
    archive set + the one-time backup KEY. Those bytes and that key must NEVER
    transit the app process — a compromised app could otherwise capture the key
    that decrypts every backup. So Caddy routes ``/restore-upload/*`` straight
    here; the app only mints time-boxed *grants* (a sha256 + expiry, never the
    archive and never the key). This service holds NO database handle, NO
    docker.sock, and NO secret mount: it validates a grant, streams a bounded,
    allowlisted file into the ``restore_inbox`` volume, and nothing else. The
    sidecar's ``restore.sh`` then reads that inbox exactly as for a hand-``scp``'d
    archive set — this container is a drop-in for that manual step.

Route: ``PUT /restore-upload/<filename>`` (Caddy uses ``handle`` — NOT
``handle_path`` — so the prefix is preserved and this service owns its full
public path). Every other method/path is rejected. No TLS (Caddy terminates).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROUTE_PREFIX = "/restore-upload/"
_PORT = 8090
_READ_CHUNK = 1024 * 1024
# Free-space margin required beyond the declared upload size (leaves headroom for
# the sidecar's decrypt/extract staging that a restore performs next to the file).
_FREE_MARGIN_BYTES = 1024**3

# Allowlist mirrors scripts/restore.sh:valid_archive_name (the five archive shapes)
# PLUS the per-restore ``manifest_<ts>.json`` and its ``.hmac`` signature (an off-host
# restore requires both) and the literal one-time ``operator_key``. Pins the whole
# string; the timestamp groups are exactly ``\d{8}_\d{6}`` as in the restore regex, so a
# tampered name cannot match.
_TS = r"\d{8}_\d{6}"
_FILENAME_RE = re.compile(
    rf"^(?:jarvis_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|litellm_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|pdfs_{_TS}\.tar\.gz(?:\.enc)?"
    rf"|secrets_{_TS}\.tar\.gz(?:\.enc)?"
    rf"|qdrant_[A-Za-z0-9_-]+_{_TS}\.snapshot(?:\.enc)?"
    rf"|manifest_{_TS}\.json(?:\.hmac)?"
    rf"|operator_key)$"
)


class _TooLargeError(Exception):
    """Streamed body exceeded the hard byte ceiling (a lying/absent Content-Length)."""


class _RejectError(Exception):
    """A request-validation failure carrying the HTTP status + short reason to emit."""

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason


def _inbox_dir() -> str:
    return os.environ.get("RESTORE_INBOX_DIR", "/restore-inbox")


def _grant_file() -> Path:
    return Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".upload_grant.json"


def _cap_bytes() -> int:
    return int(float(os.environ.get("UPLOAD_MAX_GB", "20")) * 1024**3)


def _grant_ok(token: str) -> bool:
    """True iff ``token`` matches the app-minted grant and it has not expired.

    The grant file carries only ``sha256`` + ``expires_at`` (never the raw token),
    is re-read per request (never cached), and any missing/malformed/expired state
    denies rather than raising — this endpoint never returns 500 on grant failure.
    """
    try:
        data = json.loads(_grant_file().read_text())
        stored = data["sha256"]
        expires_at = data["expires_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(stored, str) or not isinstance(expires_at, str):
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if datetime.now(UTC) >= exp:
        return False
    presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(presented, stored)


def _has_free_space(need: int) -> bool:
    """Require ``need`` + a margin free on the inbox filesystem; True if uncheckable."""
    try:
        st = os.statvfs(_inbox_dir())
    except OSError:
        return True
    return st.f_bavail * st.f_frsize >= need + _FREE_MARGIN_BYTES


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # One structured line per request to stdout; NEVER the grant token or body bytes.
    def log_message(
        self, _fmt: str, *_args: object
    ) -> None:  # silence default logging (override args unused by design)
        return

    def _emit(self, code: int, filename: str) -> None:
        sys.stdout.write(f"[uploader] {self.command} {filename} -> {code}\n")
        sys.stdout.flush()

    def _send(self, code: int, obj: dict[str, str]) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code: int, reason: str, filename: str) -> None:
        self._send(code, {"error": reason})
        self._emit(code, filename)

    def _reject_method(self) -> None:
        self._deny(405, "method not allowed", "<none>")

    do_GET = _reject_method
    do_HEAD = _reject_method
    do_POST = _reject_method
    do_DELETE = _reject_method
    do_PATCH = _reject_method
    do_OPTIONS = _reject_method

    def _stream_body(self, out, content_length: int | None, cap: int) -> int:
        """Stream the request body to ``out``, bounded by ``cap`` bytes; return the count.

        Content-Length (always set by Caddy) is honoured for exact reads on a
        keep-alive connection; when it is absent/lying the hard ceiling still
        bounds the read and raises ``_TooLargeError`` past ``cap`` — an unbounded body
        can never slip through. The caller compares the returned count against
        Content-Length to reject a truncated upload before committing it.
        """
        total = 0
        remaining = content_length if content_length is not None else cap + 1
        while remaining > 0:
            chunk = self.rfile.read(min(_READ_CHUNK, remaining))
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise _TooLargeError
            out.write(chunk)
            if content_length is not None:
                remaining -= len(chunk)
        return total

    def _resolve_filename(self) -> str:
        """Extract + validate the target filename from the request path (raises _RejectError)."""
        path = self.path.split("?", 1)[0]
        if not path.startswith(_ROUTE_PREFIX):
            raise _RejectError(404, "not found")
        filename = urllib.parse.unquote(path[len(_ROUTE_PREFIX) :])
        # Defense-in-depth: the last path segment only — reject any separator,
        # traversal, NUL or empty even though the allowlist already excludes them.
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or ".." in filename
            or "\x00" in filename
        ):
            raise _RejectError(400, "invalid filename")
        # fullmatch (not match): the pattern is $-anchored, but re `$` also matches
        # just before a terminal newline, so `.match` would accept "operator_key\n"
        # (from a %0A-encoded path). fullmatch requires the whole string to be
        # consumed, rejecting a trailing newline on this security boundary.
        if not _FILENAME_RE.fullmatch(filename):
            raise _RejectError(400, "disallowed name")
        return filename

    def _authorize(self) -> None:
        """Verify the X-Upload-Grant header against the app-minted grant (raises _RejectError)."""
        token = self.headers.get("X-Upload-Grant")
        if not token:
            raise _RejectError(401, "missing grant")
        if not _grant_ok(token):
            raise _RejectError(403, "invalid grant")

    def _checked_length(self, cap: int) -> int | None:
        """Parse + bound-check Content-Length; None when absent (raises _RejectError)."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return None
        try:
            content_length = int(raw_len)
        except ValueError:
            raise _RejectError(400, "bad content-length") from None
        if content_length < 0:
            raise _RejectError(400, "bad content-length")
        if content_length > cap:
            raise _RejectError(413, "too large")
        if not _has_free_space(content_length):
            raise _RejectError(507, "insufficient storage")
        return content_length

    def _store(self, filename: str, content_length: int | None, cap: int) -> None:
        """Stream the body into the inbox atomically, then respond (handles its own errors)."""
        inbox = Path(_inbox_dir())
        part = inbox / f"{filename}.part"
        final = inbox / filename
        try:
            with open(part, "wb") as out:
                written = self._stream_body(out, content_length, cap)
            if content_length is not None and written != content_length:
                # Client under-delivered a declared Content-Length: reject the
                # truncated archive here instead of committing a short file that
                # would only fail later at restore (sha256/decrypt).
                part.unlink(missing_ok=True)
                self.close_connection = True
                self._deny(400, "incomplete body", filename)
                return
            os.chmod(part, 0o600)
            os.replace(part, final)
        except _TooLargeError:
            part.unlink(missing_ok=True)
            self.close_connection = True
            self._deny(413, "too large", filename)
            return
        except OSError:
            part.unlink(missing_ok=True)
            self._deny(507, "write failed", filename)
            return
        self._send(201, {"status": "stored", "filename": filename})
        self._emit(201, filename)

    def do_PUT(self) -> None:
        cap = _cap_bytes()
        filename = "<none>"
        try:
            filename = self._resolve_filename()
            self._authorize()
            content_length = self._checked_length(cap)
        except _RejectError as rej:
            self._deny(rej.code, rej.reason, filename)
            return
        self._store(filename, content_length, cap)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", _PORT), UploadHandler)
    sys.stdout.write(f"[uploader] listening on 0.0.0.0:{_PORT}\n")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
