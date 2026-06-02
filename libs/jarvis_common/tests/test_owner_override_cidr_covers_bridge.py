"""Config-integrity guard: the compose OWNER_OVERRIDE_ALLOWED_CIDRS default must
cover the jarvis bridge subnet, or the Telegram bot's per-user (X-Owner-User-Id)
service calls 403 on a default deploy.

Regression guard for the audit finding C1: the bare code default is now
deny-by-default loopback-only (127.0.0.0/8), which does NOT cover the jarvis
bridge (JARVIS_NET_SUBNET, default 10.137.241.0/24), so docker-compose.yml must
set OWNER_OVERRIDE_ALLOWED_CIDRS to track JARVIS_NET_SUBNET. This test fails if
that wiring is removed or stops tracking the subnet — a failure mode no
integration test catches (they all monkeypatch the IP allowlist guard to True).
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


def _owner_override_line(text: str) -> str:
    for line in text.splitlines():
        if "OWNER_OVERRIDE_ALLOWED_CIDRS:" in line and "${" in line:
            return line
    raise AssertionError(
        "docker-compose.yml shared-env must set OWNER_OVERRIDE_ALLOWED_CIDRS "
        "(absent → the bare code default applies, which does not cover the bridge)"
    )


def test_compose_owner_override_tracks_the_bridge_subnet() -> None:
    text = _COMPOSE.read_text()
    subnet = _net_subnet_default(text)
    owner_line = _owner_override_line(text)

    # It must reference JARVIS_NET_SUBNET so the allowlist FOLLOWS the bridge
    # subnet (including operator overrides of JARVIS_NET_SUBNET).
    assert "JARVIS_NET_SUBNET" in owner_line, (
        "OWNER_OVERRIDE_ALLOWED_CIDRS must track ${JARVIS_NET_SUBNET} so the bot "
        f"is trusted regardless of the bridge subnet; got: {owner_line.strip()}"
    )

    # Belt-and-suspenders: resolve the default allowlist and prove the default
    # bridge subnet is actually covered (catches a malformed reference).
    resolved = owner_line.replace("${JARVIS_NET_SUBNET:-" + subnet + "}", subnet)
    m = re.search(r"OWNER_OVERRIDE_ALLOWED_CIDRS:-([^}]+)\}", resolved)
    assert m, (
        f"could not resolve the OWNER_OVERRIDE_ALLOWED_CIDRS default from: {owner_line.strip()}"
    )
    allowlist = [ipaddress.IPv4Network(c.strip()) for c in m.group(1).split(",") if c.strip()]

    bridge = ipaddress.IPv4Network(subnet)
    assert any(bridge.subnet_of(net) for net in allowlist), (
        f"jarvis bridge subnet {subnet} is not covered by the owner-override "
        f"allowlist {[str(n) for n in allowlist]} — the bot would 403"
    )


def test_compose_resolved_allowlist_covers_a_bridge_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the compose-injected env (loopback + bridge), an IP inside the
    bridge subnet is allowed — owner-override still works on a compose deploy."""
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,10.137.241.0/24")
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    auth.refresh_allowed_networks_cache()

    assert auth._ip_in_allowlist("10.137.241.5"), (
        "a containerized bot on the jarvis bridge must be trusted when compose "
        "injects the bridge subnet into OWNER_OVERRIDE_ALLOWED_CIDRS"
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
