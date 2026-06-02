"""Config-integrity guard: the compose OWNER_OVERRIDE_ALLOWED_CIDRS default must
cover the jarvis bridge subnet, or the Telegram bot's per-user (X-Owner-User-Id)
service calls 403 on a default deploy.

Regression guard for the audit finding C1: the bare code default
(127.0.0.0/8,172.16.0.0/12) does NOT cover the jarvis bridge
(JARVIS_NET_SUBNET, default 10.137.241.0/24), so docker-compose.yml must set
OWNER_OVERRIDE_ALLOWED_CIDRS to track JARVIS_NET_SUBNET. This test fails if that
wiring is removed or stops tracking the subnet — a failure mode no integration
test catches (they all monkeypatch the IP allowlist guard to True).
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

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
    allowlist = [ipaddress.ip_network(c.strip()) for c in m.group(1).split(",") if c.strip()]

    bridge = ipaddress.ip_network(subnet)
    assert any(bridge.subnet_of(net) for net in allowlist), (
        f"jarvis bridge subnet {subnet} is not covered by the owner-override "
        f"allowlist {[str(n) for n in allowlist]} — the bot would 403"
    )
