"""Config-integrity guard: the compose OWNER_OVERRIDE_ALLOWED_CIDRS default must
cover the Telegram bot's pinned address and NOTHING else on the jarvis bridge.

The X-Owner-User-Id override lets its caller act as any user, so the only
container entitled to send it is the bot. The bot has a pinned ipv4_address
(JARVIS_TELEGRAM_BOT_IP, derived by setup from JARVIS_NET_SUBNET), and
docker-compose.yml must set OWNER_OVERRIDE_ALLOWED_CIDRS to that address as a
/32. This test fails if the wiring is removed, stops tracking the bot pin, or
widens back to the whole bridge subnet — where any sibling container could
impersonate any user. It is a failure mode no integration test catches (they
all monkeypatch the IP allowlist guard to True).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from pathlib import Path

import pytest
from jarvis_common import auth

_COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _net_subnet_default(text: str) -> str:
    m = re.search(r"JARVIS_NET_SUBNET:-([\d./]+)", text)
    assert m, "JARVIS_NET_SUBNET default not found in docker-compose.yml"
    return m.group(1)


def _telegram_bot_ip_default(text: str) -> str:
    m = re.search(r"JARVIS_TELEGRAM_BOT_IP:-([\d.]+)", text)
    assert m, "JARVIS_TELEGRAM_BOT_IP default not found in docker-compose.yml"
    return m.group(1)


def _assert_compose_var_tracks_bot_pin(text: str, subnet: str, bot_ip: str, var_name: str) -> None:
    """Assert the compose default for `var_name` (a `${VAR:-a,b,c}` CIDR list)
    references JARVIS_TELEGRAM_BOT_IP and, once resolved, covers the bot's
    pinned address while leaving the rest of the jarvis bridge untrusted."""
    for line in text.splitlines():
        if f"{var_name}:" in line and "${" in line:
            var_line = line
            break
    else:
        raise AssertionError(
            f"docker-compose.yml shared-env must set {var_name} "
            "(absent → the bare code default applies, which does not cover the bot)"
        )

    # It must reference JARVIS_TELEGRAM_BOT_IP so the allowlist FOLLOWS the
    # bot's pin (including operator overrides of JARVIS_NET_SUBNET, from which
    # setup derives that pin).
    assert "JARVIS_TELEGRAM_BOT_IP" in var_line, (
        f"{var_name} must track ${{JARVIS_TELEGRAM_BOT_IP}} so the bot hop is "
        f"trusted regardless of the bridge subnet; got: {var_line.strip()}"
    )

    # Resolve the nested ${JARVIS_TELEGRAM_BOT_IP:-...} default before the
    # regex below, which stops at the first closing brace.
    resolved = var_line.replace("${JARVIS_TELEGRAM_BOT_IP:-" + bot_ip + "}", bot_ip)
    m = re.search(rf"{var_name}:-([^}}]+)\}}", resolved)
    assert m, f"could not resolve the {var_name} default from: {var_line.strip()}"
    entries = [e.strip() for e in m.group(1).split(",") if e.strip()]
    allowlist = [ipaddress.IPv4Network(e) for e in entries]

    bot = ipaddress.IPv4Address(bot_ip)
    assert any(bot in net for net in allowlist), (
        f"the Telegram bot's pinned address {bot_ip} is not covered by {var_name} "
        f"{[str(n) for n in allowlist]} — its per-user calls would 403"
    )

    bridge = ipaddress.IPv4Network(subnet)
    assert not any(bridge.subnet_of(net) for net in allowlist), (
        f"jarvis bridge subnet {subnet} is covered by {var_name} "
        f"{[str(n) for n in allowlist]} — any sibling container could act as any user"
    )


def test_compose_owner_override_tracks_the_bot_pin() -> None:
    text = _COMPOSE.read_text()
    _assert_compose_var_tracks_bot_pin(
        text,
        _net_subnet_default(text),
        _telegram_bot_ip_default(text),
        "OWNER_OVERRIDE_ALLOWED_CIDRS",
    )


def test_compose_resolved_allowlist_trusts_only_the_bot_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the compose-injected env (loopback + the bot's /32), the bot's
    pinned address is allowed and an ordinary bridge peer is not."""
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,10.137.241.250/32")
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    auth.refresh_allowed_networks_cache()

    assert auth._ip_in_allowlist("10.137.241.250"), (
        "the containerized bot must be trusted when compose injects its pinned "
        "address into OWNER_OVERRIDE_ALLOWED_CIDRS"
    )
    assert not auth._ip_in_allowlist("10.137.241.5"), (
        "another container on the jarvis bridge must NOT be able to send "
        "X-Owner-User-Id — that is impersonation of any user"
    )
    assert auth._ip_in_allowlist("127.0.0.1")


def test_code_default_is_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare code default (no env override) is loopback-only: a bridge IP is
    NOT covered. Non-compose runs are deny-by-default for non-loopback callers."""
    monkeypatch.delenv("OWNER_OVERRIDE_ALLOWED_CIDRS", raising=False)
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    auth.refresh_allowed_networks_cache()

    assert auth._ip_in_allowlist("127.0.0.1"), "loopback must always be allowed"
    assert not auth._ip_in_allowlist("10.137.241.5"), (
        "the loopback-only code default must NOT cover a bridge IP"
    )


def test_startup_warning_fires_for_loopback_only_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """refresh_allowed_networks_cache warns once when the loopback-only default
    is in use, and does NOT warn when the operator widens the allowlist."""
    # Case 1: loopback-only default in use → warning fires once.
    monkeypatch.delenv("OWNER_OVERRIDE_ALLOWED_CIDRS", raising=False)
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    monkeypatch.setattr(auth, "_LOOPBACK_DEFAULT_WARNED", False)

    with caplog.at_level(logging.WARNING, logger=auth.logger.name):
        auth.refresh_allowed_networks_cache()
        auth.refresh_allowed_networks_cache()  # must not double-fire

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "OWNER_OVERRIDE_ALLOWED_CIDRS" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        f"loopback-only default must warn exactly once at startup; got {len(warnings)}"
    )
    assert "loopback" in warnings[0].getMessage().lower()

    # Case 2: operator widened the allowlist → no warning.
    caplog.clear()
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,10.137.241.0/24")
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    monkeypatch.setattr(auth, "_LOOPBACK_DEFAULT_WARNED", False)

    with caplog.at_level(logging.WARNING, logger=auth.logger.name):
        auth.refresh_allowed_networks_cache()

    assert not [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "OWNER_OVERRIDE_ALLOWED_CIDRS" in r.getMessage()
    ], "no loopback warning when the operator widens the allowlist"


def test_compose_trusted_proxy_hosts_pins_only_the_dashboard() -> None:
    """Proxy-header trust is the exact dashboard hop, not every bridge peer.

    OWNER_OVERRIDE_ALLOWED_CIDRS pins the bot's own address for direct bot
    calls; TRUSTED_PROXY_HOSTS has a different purpose and must not let a
    sibling container rewrite the client IP seen by the owner-override guard.
    """
    text = _COMPOSE.read_text()
    line = next(
        (line for line in text.splitlines() if "TRUSTED_PROXY_HOSTS:" in line),
        "",
    )
    assert "${JARVIS_DASHBOARD_IP:-10.137.241.253}/32" in line
    assert "JARVIS_NET_SUBNET" not in line
    assert "10.137.241.0/24" not in line
