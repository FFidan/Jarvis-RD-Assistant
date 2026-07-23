"""Config-integrity guard: the compose OWNER_OVERRIDE_ALLOWED_CIDRS default must
cover the jarvis bridge subnet, or the Telegram bot's per-user (X-Owner-User-Id)
service calls 403 on a default deploy.

Regression guard: the bare code default is now
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


def _assert_compose_var_tracks_bridge_subnet(text: str, subnet: str, var_name: str) -> None:
    """Assert the compose default for `var_name` (a `${VAR:-a,b,c}` CIDR list)
    references JARVIS_NET_SUBNET and, once resolved, actually covers the
    jarvis bridge subnet — belt-and-suspenders against a malformed reference."""
    for line in text.splitlines():
        if f"{var_name}:" in line and "${" in line:
            var_line = line
            break
    else:
        raise AssertionError(
            f"docker-compose.yml shared-env must set {var_name} "
            "(absent → the bare code default applies, which does not cover the bridge)"
        )

    # It must reference JARVIS_NET_SUBNET so the allowlist FOLLOWS the bridge
    # subnet (including operator overrides of JARVIS_NET_SUBNET).
    assert "JARVIS_NET_SUBNET" in var_line, (
        f"{var_name} must track ${{JARVIS_NET_SUBNET}} so the bridge hop is "
        f"trusted regardless of the bridge subnet; got: {var_line.strip()}"
    )

    # Belt-and-suspenders: resolve the default allowlist and prove the default
    # bridge subnet is actually covered (catches a malformed reference).
    resolved = var_line.replace("${JARVIS_NET_SUBNET:-" + subnet + "}", subnet)
    m = re.search(rf"{var_name}:-([^}}]+)\}}", resolved)
    assert m, f"could not resolve the {var_name} default from: {var_line.strip()}"
    entries = [e.strip() for e in m.group(1).split(",") if e.strip()]
    allowlist = [ipaddress.IPv4Network(e) for e in entries]

    bridge = ipaddress.IPv4Network(subnet)
    assert any(bridge.subnet_of(net) for net in allowlist), (
        f"jarvis bridge subnet {subnet} is not covered by {var_name} "
        f"{[str(n) for n in allowlist]} — the bridge hop would not be trusted"
    )


def test_compose_owner_override_tracks_the_bridge_subnet() -> None:
    text = _COMPOSE.read_text()
    subnet = _net_subnet_default(text)
    _assert_compose_var_tracks_bridge_subnet(text, subnet, "OWNER_OVERRIDE_ALLOWED_CIDRS")


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


def test_compose_trusted_proxy_hosts_pins_only_the_dashboard() -> None:
    """Proxy-header trust is the exact dashboard hop, not every bridge peer.

    OWNER_OVERRIDE_ALLOWED_CIDRS still covers the bridge for direct bot calls;
    TRUSTED_PROXY_HOSTS has a narrower purpose and must not let a sibling
    container rewrite the client IP seen by the owner-override guard.
    """
    text = _COMPOSE.read_text()
    line = next(
        (line for line in text.splitlines() if "TRUSTED_PROXY_HOSTS:" in line),
        "",
    )
    assert "${JARVIS_DASHBOARD_IP:-10.137.241.253}/32" in line
    assert "JARVIS_NET_SUBNET" not in line
    assert "10.137.241.0/24" not in line
