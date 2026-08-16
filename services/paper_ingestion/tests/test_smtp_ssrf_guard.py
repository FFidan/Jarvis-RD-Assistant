"""Adversarial unit tests for the SMTP-test SSRF guard in routers/setup.py.

These call ``_reject_non_public_host`` / ``_send_test_email`` DIRECTLY (no HTTP
handler), so they live in a plain unit file rather than tests/contract/ — the
contract-shape guard (docs/contracts/07-testing.md) requires contract files
to drive the HTTP handler. The HTTP-driven SSRF assertion lives in
contract/test_setup_contract.py::test_smtp_test_send_rejects_private_host.

Covers the otherwise-untested guard branches:
  a) public host → does NOT raise; send IS attempted (no overzealous blocking)
  b) multi-IP, any private → raises (split-horizon DNS rebinding mitigation)
  c) getaddrinfo OSError → ValueError, host string NOT reflected (no probe oracle)
  d) 0.0.0.0 (unspecified) → raises

The guard resolves via ``asyncio.get_running_loop().getaddrinfo`` (async), so the
loop's bound method is patched on the live event loop — not ``socket.getaddrinfo``.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from unittest.mock import AsyncMock

import pytest
from platform_api.routers.setup import (
    SmtpBody,
    _reject_non_public_host,
    _send_test_email,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _addrinfo(ip_str: str) -> list[tuple[Any, ...]]:
    """One getaddrinfo 5-tuple for the given IP. info[4] is the sockaddr, [4][0] the IP."""
    family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 0, "", (ip_str, 0))]


def _patch_getaddrinfo(monkeypatch: pytest.MonkeyPatch, result: list[tuple[Any, ...]]) -> None:
    """Patch the running loop's getaddrinfo to return ``result``."""
    loop = asyncio.get_running_loop()

    async def fake_gai(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return result

    monkeypatch.setattr(loop, "getaddrinfo", fake_gai)


def _patch_getaddrinfo_raises(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Patch the running loop's getaddrinfo to raise ``exc``."""
    loop = asyncio.get_running_loop()

    async def fake_gai(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        raise exc

    monkeypatch.setattr(loop, "getaddrinfo", fake_gai)


# ---------------------------------------------------------------------------
# a) public host happy path — guard does not raise; send IS attempted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_host_passes_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public IP must NOT be rejected (guard must not block legitimate sends)."""
    _patch_getaddrinfo(monkeypatch, _addrinfo("8.8.8.8"))
    # Should not raise.
    await _reject_non_public_host("smtp.example.com")


@pytest.mark.asyncio
async def test_public_host_send_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through _send_test_email, a public host with a working relay returns None."""
    _patch_getaddrinfo(monkeypatch, _addrinfo("8.8.8.8"))

    import aiosmtplib

    mock_send = AsyncMock(name="aiosmtplib.send")
    monkeypatch.setattr(aiosmtplib, "send", mock_send)
    monkeypatch.setattr(
        "platform_api.routers.setup.connect_pinned_socket",
        AsyncMock(return_value=socket.socket()),
    )

    body = SmtpBody(host="smtp.example.com", port=587, from_email="a@b.com")
    result = await _send_test_email(body, "a@b.com", None)

    assert result is None, f"public-host send should succeed (None); got: {result!r}"
    mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# a') TLS mode is chosen by port: 465 implicit TLS, 587 STARTTLS, 25 plaintext
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "expected_use_tls", "expected_start_tls"),
    [
        (25, False, False),  # plain relay — must NOT force STARTTLS
        (587, False, True),  # submission — STARTTLS upgrade
        (465, True, False),  # SMTPS — implicit TLS on connect
    ],
)
@pytest.mark.asyncio
async def test_send_test_email_tls_flags_by_port(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    expected_use_tls: bool,
    expected_start_tls: bool,
) -> None:
    """_send_test_email maps the port to the right aiosmtplib TLS flags.

    Port 25 must leave both flags False (a previous ``start_tls = not use_tls``
    forced STARTTLS on plain relays, which they reject).
    """
    _patch_getaddrinfo(monkeypatch, _addrinfo("8.8.8.8"))

    import aiosmtplib

    mock_send = AsyncMock(name="aiosmtplib.send")
    monkeypatch.setattr(aiosmtplib, "send", mock_send)
    monkeypatch.setattr(
        "platform_api.routers.setup.connect_pinned_socket",
        AsyncMock(return_value=socket.socket()),
    )

    body = SmtpBody(host="smtp.example.com", port=port, from_email="a@b.com")
    result = await _send_test_email(body, "a@b.com", None)

    assert result is None
    kwargs = mock_send.call_args.kwargs
    assert kwargs["use_tls"] is expected_use_tls
    assert kwargs["start_tls"] is expected_start_tls
    assert kwargs["port"] is None
    assert kwargs["sock"] is not None


@pytest.mark.asyncio
async def test_all_smtp_callers_use_a_real_pinned_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe, setup send, and magic-link send complete through one fake relay."""
    from jarvis_common.email import (
        MagicLinkDelivery,
        _EffectiveSmtp,
        _probe_relay,
        send_magic_link,
    )
    from jarvis_common.settings import get_secrets_settings

    commands: list[str] = []
    connections = 0

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        writer.write(b"220 fake-relay ESMTP\r\n")
        await writer.drain()
        try:
            while line := await reader.readline():
                command = line.decode("ascii", errors="replace").strip()
                commands.append(command.split(" ", 1)[0].upper())
                if command.upper().startswith(("EHLO", "HELO")):
                    writer.write(b"250-fake-relay\r\n250 SIZE 100000\r\n")
                elif command.upper() == "DATA":
                    writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    await writer.drain()
                    await reader.readuntil(b"\r\n.\r\n")
                    writer.write(b"250 accepted\r\n")
                elif command.upper() == "QUIT":
                    writer.write(b"221 closing\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 ok\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(relay, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_PORT", str(port))
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    monkeypatch.delenv("DEV_SMTP_LOG_ONLY", raising=False)
    get_secrets_settings.cache_clear()
    try:
        body = SmtpBody(host="localhost", port=port, from_email="bot@example.com")
        assert await _send_test_email(body, "researcher@example.com", None) is None
        assert await _probe_relay(
            _EffectiveSmtp(
                host="localhost",
                port=port,
                user=None,
                password=None,
                sender="bot@example.com",
            )
        ) == (True, None)
        assert (
            await send_magic_link(
                "researcher@example.com",
                "https://example.com/verify?token=test",
                pool=None,
            )
            is MagicLinkDelivery.DELIVERED
        )
    finally:
        get_secrets_settings.cache_clear()
        server.close()
        await server.wait_closed()

    assert connections == 3
    assert commands.count("DATA") == 2
    assert commands.count("QUIT") == 3


# ---------------------------------------------------------------------------
# b) multi-IP — any private/loopback record causes rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_ip_any_private_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If resolution yields a public AND a loopback record, the host is rejected.

    Split-horizon / DNS-rebinding mitigation: a single private answer poisons the set.
    """
    _patch_getaddrinfo(monkeypatch, _addrinfo("8.8.8.8") + _addrinfo("127.0.0.1"))
    with pytest.raises(ValueError, match="non-public"):
        await _reject_non_public_host("rebind.example.com")


# ---------------------------------------------------------------------------
# c) unresolvable host — OSError → ValueError, host NOT reflected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_host_raises_without_reflecting_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """getaddrinfo OSError → ValueError('Could not resolve SMTP host'); host string absent.

    The attacker-supplied host must not appear in the message (no reflection oracle).
    """
    secret_host = "internal-probe-target.attacker.example"
    _patch_getaddrinfo_raises(monkeypatch, OSError("nodename nor servname provided"))
    with pytest.raises(ValueError, match="Could not resolve SMTP host") as exc_info:
        await _reject_non_public_host(secret_host)
    assert secret_host not in str(exc_info.value), (
        f"raised message must not reflect the supplied host; got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# d) 0.0.0.0 (unspecified) — rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unspecified_address_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.0.0.0 (is_unspecified) must be rejected explicitly."""
    _patch_getaddrinfo(monkeypatch, _addrinfo("0.0.0.0"))
    with pytest.raises(ValueError, match="non-public"):
        await _reject_non_public_host("zero.example.com")
