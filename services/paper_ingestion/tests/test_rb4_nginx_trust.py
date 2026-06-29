"""Regression test: pinned jarvis subnet + Caddy /32 nginx real_ip trust.

Verifies that:
  (a) nginx.conf trusts exactly 10.137.241.2/32, 10.137.241.3/32, and 127.0.0.1
      via set_real_ip_from directives.
  (b) nginx.conf no longer contains the old broad-CIDR trust surface
      (172.16.0.0/12 or the NGINX_TRUSTED_PROXY_CIDR variable reference).
  (c) docker-compose.yml networks block has the pinned subnet
      (10.137.241.0/24) and the two Caddy static IPs (.2 and .3).
  (d) docker-compose.yml dashboard service no longer passes NGINX_TRUSTED_PROXY_CIDR.
  (e) docker-compose.yml parses as valid YAML and (if docker CLI is present)
      passes `docker compose config -q`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO_ROOT / "frontend" / "nginx.conf"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nginx_text() -> str:
    return NGINX_CONF.read_text()


def _compose_text() -> str:
    return COMPOSE_FILE.read_text()


# ---------------------------------------------------------------------------
# nginx.conf assertions
# ---------------------------------------------------------------------------


def test_nginx_trusts_caddy_letsencrypt_ip():
    """nginx must trust the letsencrypt Caddy container's static /32."""
    assert "set_real_ip_from 10.137.241.2/32;" in _nginx_text(), (
        "nginx.conf is missing 'set_real_ip_from 10.137.241.2/32;'"
    )


def test_nginx_trusts_caddy_local_ip():
    """nginx must trust the caddy-local container's static /32."""
    assert "set_real_ip_from 10.137.241.3/32;" in _nginx_text(), (
        "nginx.conf is missing 'set_real_ip_from 10.137.241.3/32;'"
    )


def test_nginx_trusts_loopback():
    """nginx must retain the loopback trust (bootstrap / health-probe path)."""
    assert "set_real_ip_from 127.0.0.1;" in _nginx_text(), (
        "nginx.conf is missing 'set_real_ip_from 127.0.0.1;'"
    )


def test_nginx_has_real_ip_header_directive():
    """real_ip_header X-Real-IP must be present."""
    assert "real_ip_header X-Real-IP;" in _nginx_text(), (
        "nginx.conf is missing 'real_ip_header X-Real-IP;'"
    )


def test_nginx_does_not_trust_broad_cidr():
    """The old broad Docker bridge /12 must not appear in a set_real_ip_from directive."""
    for line in _nginx_text().splitlines():
        stripped = line.strip()
        assert not (stripped.startswith("set_real_ip_from") and "172.16.0.0/12" in stripped), (
            f"nginx.conf has a set_real_ip_from directive trusting 172.16.0.0/12: {line!r}"
        )


def test_nginx_does_not_reference_nginx_trusted_proxy_cidr_var():
    """No set_real_ip_from directive may use the NGINX_TRUSTED_PROXY_CIDR variable."""
    for line in _nginx_text().splitlines():
        stripped = line.strip()
        assert not (
            stripped.startswith("set_real_ip_from") and "NGINX_TRUSTED_PROXY_CIDR" in stripped
        ), (
            f"nginx.conf still has a set_real_ip_from line referencing NGINX_TRUSTED_PROXY_CIDR: {line!r}"
        )


def test_nginx_serves_mjs_assets_with_javascript_mime():
    """Vite .mjs worker assets must not fall back to octet-stream."""
    text = _nginx_text()
    mjs_location = (
        "location ~* \\.mjs$ {\n"
        "        include /etc/nginx/nginx-security-headers.conf;\n"
        "        default_type application/javascript;\n"
        "        try_files $uri =404;\n"
        '        add_header Cache-Control "public, max-age=31536000, immutable";'
    )
    assert mjs_location in text
    assert text.index("location ~* \\.mjs$") < text.index(
        "location ~* \\.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$"
    )


# ---------------------------------------------------------------------------
# docker-compose.yml assertions
# ---------------------------------------------------------------------------


def test_compose_networks_has_pinned_subnet():
    """The jarvis network must declare the pinned 10.137.241.0/24 subnet."""
    assert "subnet: ${JARVIS_NET_SUBNET:-10.137.241.0/24}" in _compose_text(), (
        "docker-compose.yml networks block is missing the pinned subnet"
    )


def test_compose_caddy_letsencrypt_has_static_ip():
    """The caddy (letsencrypt) service must have ipv4_address 10.137.241.2."""
    assert "ipv4_address: 10.137.241.2" in _compose_text(), (
        "docker-compose.yml caddy service is missing ipv4_address: 10.137.241.2"
    )


def test_compose_caddy_local_has_static_ip():
    """The caddy_local service must have ipv4_address 10.137.241.3."""
    assert "ipv4_address: 10.137.241.3" in _compose_text(), (
        "docker-compose.yml caddy_local service is missing ipv4_address: 10.137.241.3"
    )


def test_compose_dashboard_has_no_nginx_trusted_proxy_cidr():
    """dashboard service must not pass NGINX_TRUSTED_PROXY_CIDR to nginx."""
    assert "NGINX_TRUSTED_PROXY_CIDR:" not in _compose_text(), (
        "docker-compose.yml still has NGINX_TRUSTED_PROXY_CIDR in dashboard env"
    )


# ---------------------------------------------------------------------------
# YAML validity + docker compose config
# ---------------------------------------------------------------------------


def test_compose_is_valid_yaml():
    """docker-compose.yml must parse as valid YAML."""
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        pytest.skip("pyyaml not installed; skipping YAML parse check")

    yaml.safe_load(COMPOSE_FILE.read_text())  # raises on parse error


def test_compose_config_valid():
    """docker compose config -q must succeed (schema + env-var resolution)."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"docker compose config -q failed (exit {result.returncode}):\n{result.stderr}"
    )
