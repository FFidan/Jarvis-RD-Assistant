"""Regression tests for the pinned ingress trust boundary.

Verifies that:
  (a) nginx trusts only the parameterized host gateway and edge peers.
  (b) nginx.conf no longer contains the old broad-CIDR trust surface
      (172.16.0.0/12 or the NGINX_TRUSTED_PROXY_CIDR variable reference).
  (c) Compose parameterizes the gateway, Caddy, dashboard, and cloudflared IPs.
  (d) docker-compose.yml dashboard service no longer passes NGINX_TRUSTED_PROXY_CIDR.
  (e) docker-compose.yml parses as valid YAML and (if docker CLI is present)
      passes `docker compose config -q`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO_ROOT / "frontend" / "nginx.conf"
NGINX_RATE_LIMIT_CONF = REPO_ROOT / "frontend" / "nginx-rate-limit.conf"
NGINX_SECURITY_HEADERS_CONF = REPO_ROOT / "frontend" / "nginx-security-headers.conf"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
CADDY_LOCAL_FILE = REPO_ROOT / "caddy" / "Caddyfile.local"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nginx_text() -> str:
    return NGINX_CONF.read_text()


def _nginx_rate_limit_text() -> str:
    return NGINX_RATE_LIMIT_CONF.read_text()


def _nginx_security_headers_text() -> str:
    return NGINX_SECURITY_HEADERS_CONF.read_text()


def _compose_text() -> str:
    return COMPOSE_FILE.read_text()


def _caddy_local_text() -> str:
    return CADDY_LOCAL_FILE.read_text()


def _location_blocks(text: str) -> list[str]:
    """Return the body text of every top-level `location ... { ... }` block."""
    blocks: list[str] = []
    for match in re.finditer(r"^\s*location\b[^{]*\{", text, flags=re.MULTILINE):
        depth = 1
        pos = match.end()
        while depth > 0:
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        blocks.append(text[match.end() : pos - 1])
    return blocks


# ---------------------------------------------------------------------------
# nginx.conf assertions
# ---------------------------------------------------------------------------


def test_nginx_access_log_never_records_url_or_referer_secrets():
    """Access logs keep diagnostics but omit every query-bearing field.

    Magic-link and account-confirmation tokens are bearer credentials.  The
    request line, request URI, arguments, and Referer can all contain them, so
    none belongs in the dashboard container's access log.
    """
    text = _nginx_rate_limit_text()
    assert "$request_method" in text
    assert "$uri" in text
    assert "$server_protocol" in text
    for unsafe_variable in ("$request", "$request_uri", "$args", "$http_referer"):
        assert re.search(rf"{re.escape(unsafe_variable)}(?![A-Za-z0-9_])", text) is None


def test_nginx_trusts_parameterized_edge_peers_only():
    """Trusted ingress is pinned to the edge containers and host gateway."""
    text = _nginx_text()
    for var in (
        "JARVIS_NET_GATEWAY_IP",
        "JARVIS_CADDY_IP",
        "JARVIS_CADDY_LOCAL_IP",
        "JARVIS_CLOUDFLARED_IP",
    ):
        assert f"set_real_ip_from ${{{var}}}/32;" in text
    assert "set_real_ip_from 10.137.241.0/24" not in text


def test_nginx_has_separate_trusted_ingress_listener():
    text = _nginx_text()
    assert "listen 3002;" in text
    assert "$realip_remote_addr" in text
    assert "jarvis_trusted_ingress_allowed" in text


def test_raw_http_allows_host_gateway_app_only_for_a_loopback_host_bind():
    """Docker Desktop/VM forwarding must not turn a LAN peer into localhost.

    Some forwarding stacks collapse every host-side connection to the Docker
    gateway address.  The gateway may therefore unlock the app only when the
    same Compose setting that publishes the port says it is loopback-bound.
    """
    text = _nginx_text()
    assert 'map "$server_port:$realip_remote_addr:${DASHBOARD_BIND_HOST}"' in text
    assert '"3000:${JARVIS_NET_GATEWAY_IP}:127.0.0.1" 1;' in text
    assert '"3000:${JARVIS_NET_GATEWAY_IP}" 1;' not in text
    assert '"3000:/health/jarvis" 1;' in text
    assert "if ($jarvis_request_allowed = 0) { return 403; }" in text


def test_compose_passes_the_dashboard_bind_setting_into_nginx():
    """One setting controls both host publication and nginx enforcement."""
    text = _compose_text()
    bind_expression = "${DASHBOARD_BIND_HOST:-127.0.0.1}"
    assert f'- "{bind_expression}:${{DASHBOARD_HOST_PORT:-3001}}:3000"' in text
    assert f"DASHBOARD_BIND_HOST: {bind_expression}" in text


def test_nginx_never_preserves_client_forwarded_proto():
    text = _nginx_text()
    assert "$http_x_forwarded_proto" not in text
    assert "proxy_set_header X-Forwarded-Proto $jarvis_forwarded_proto;" in text
    assert "3002 https;" in text


def test_nginx_rebuilds_forwarding_headers_instead_of_appending_client_input():
    text = _nginx_text()
    assert "real_ip_header X-Forwarded-For;" in text
    assert "$proxy_add_x_forwarded_for" not in text
    assert "proxy_set_header X-Forwarded-For $jarvis_client_ip;" in text
    assert 'proxy_set_header Forwarded "";' in text
    assert "proxy_set_header X-Forwarded-Host $http_host;" in text


def test_nginx_preserves_the_validated_external_port_for_generated_links():
    """localhost and other non-default origins need their Host port intact.

    Nginx's ``$host`` drops the port, producing manual invite links such as
    ``http://localhost/auth/verify`` for a dashboard actually served on :3001.
    ``$http_host`` retains the browser-facing port after the server_name
    allowlist has already validated the hostname.
    """
    proxied_blocks = [block for block in _location_blocks(_nginx_text()) if "proxy_pass" in block]
    for block in proxied_blocks:
        assert "proxy_set_header Host $http_host;" in block
        assert "proxy_set_header X-Forwarded-Host $http_host;" in block
        assert "proxy_set_header Host $host;" not in block
        assert "proxy_set_header X-Forwarded-Host $host;" not in block


def test_local_caddy_preserves_the_browser_host_and_port_for_generated_links():
    """A non-default HTTPS port must survive both proxy hops.

    Manual invite and magic-link URLs are derived from the normalized request
    origin. Replacing ``localhost:3443`` with bare ``localhost`` makes those
    links point at an unrelated port even though the current page works.
    """
    text = _caddy_local_text()
    assert "header_up Host {hostport}" in text
    assert "header_up Host localhost" not in text


def test_local_caddy_clears_hsts_instead_of_pinning_shared_localhost():
    """Local HTTPS must not disable the supported localhost HTTP fallback.

    Browsers store HSTS by hostname rather than port, so a positive policy from
    ``https://localhost:3443`` would also rewrite ``http://localhost:3001``.
    The remaining edge-owned security headers must stay in place.
    """
    text = _caddy_local_text()
    hsts_lines = [line.strip() for line in text.splitlines() if "Strict-Transport-Security" in line]
    assert hsts_lines == ['header Strict-Transport-Security "max-age=0"']
    for header in ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in text


def test_nginx_rebuilds_cloudflare_identity_from_pinned_ingress():
    """Only the pinned cloudflared peer may forward CF client identity."""
    text = _nginx_text()
    assert '"3002:${JARVIS_CLOUDFLARED_IP}" 1;' in text
    assert "$jarvis_cf_connecting_ip" in text
    proxied_blocks = [block for block in _location_blocks(text) if "proxy_pass" in block]
    for block in proxied_blocks:
        assert "proxy_set_header CF-Connecting-IP $jarvis_cf_connecting_ip;" in block
        assert "proxy_set_header X-Jarvis-CF-Ingress $jarvis_cf_ingress;" in block


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


def test_nginx_csp_allows_the_pdf_readers_same_origin_blob():
    """PDF.js loads the authenticated response through a same-origin blob URL."""
    csp = _nginx_security_headers_text()
    assert "connect-src 'self' blob:;" in csp


def test_api_rate_limit_classifies_rejections_as_429():
    """API throttling must not be indistinguishable from upstream 503 outages."""
    text = _nginx_rate_limit_text()
    assert "limit_req_zone $binary_remote_addr zone=api_zone:10m rate=10r/s;" in text
    assert "limit_req_status 429;" in text


def test_nginx_general_api_routes_absorb_spa_bursts():
    """Normal SPA API locations get burst=80 while uploads stay stricter."""
    text = _nginx_text()
    assert text.count("limit_req zone=api_zone burst=80 nodelay;") >= 5
    upload_limit = (
        "location = /api/papers/upload {\n        limit_req zone=api_zone burst=2 nodelay;"
    )
    assert upload_limit in text


def test_nginx_proxies_backend_liveness_routes():
    """The dashboard can check process liveness without deep dependency probes."""
    text = _nginx_text()
    assert "location = /health/paper_ingestion/live" in text
    assert "proxy_pass http://paper_ingestion:8000/health/live;" in text
    assert "location = /health/learning_engine/live" in text
    assert "proxy_pass http://learning_engine:8001/health/live;" in text


def test_nginx_proxied_locations_strip_owner_header():
    """Every proxied location must strip a browser-supplied X-Owner-User-Id:
    only the container-bridge bot (which never traverses nginx) may set it.
    A future location added without this line — or a deleted strip line —
    would silently reopen an owner-impersonation hole with no other signal."""
    proxied_blocks = [block for block in _location_blocks(_nginx_text()) if "proxy_pass" in block]
    assert len(proxied_blocks) >= 1, "no proxy_pass location blocks found in nginx.conf"
    for block in proxied_blocks:
        assert 'proxy_set_header X-Owner-User-Id "";' in block, (
            'a proxied location block is missing proxy_set_header X-Owner-User-Id "";:\n' + block
        )


def test_nginx_exposes_exact_static_app_marker():
    text = _nginx_text()
    assert "location = /health/jarvis" in text
    assert 'return 200 "jarvis-rd-assistant\\n";' in text


# ---------------------------------------------------------------------------
# docker-compose.yml assertions
# ---------------------------------------------------------------------------


def test_compose_networks_has_pinned_subnet():
    """The jarvis network must declare the pinned 10.137.241.0/24 subnet."""
    assert "subnet: ${JARVIS_NET_SUBNET:-10.137.241.0/24}" in _compose_text(), (
        "docker-compose.yml networks block is missing the pinned subnet"
    )


def test_compose_parameterizes_all_ingress_addresses():
    text = _compose_text()
    expected = {
        "JARVIS_NET_GATEWAY_IP": "10.137.241.1",
        "JARVIS_CADDY_IP": "10.137.241.251",
        "JARVIS_CADDY_LOCAL_IP": "10.137.241.252",
        "JARVIS_DASHBOARD_IP": "10.137.241.253",
        "JARVIS_CLOUDFLARED_IP": "10.137.241.254",
    }
    for name, default in expected.items():
        assert f"${{{name}:-{default}}}" in text


def test_compose_backend_proxy_trust_is_dashboard_only():
    text = _compose_text()
    assert (
        "TRUSTED_PROXY_HOSTS: ${TRUSTED_PROXY_HOSTS:-127.0.0.0/8,${JARVIS_DASHBOARD_IP:-10.137.241.253}/32}"
        in text
    )
    assert (
        "TRUSTED_PROXY_CIDRS: ${TRUSTED_PROXY_CIDRS:-127.0.0.0/8,${JARVIS_DASHBOARD_IP:-10.137.241.253}/32}"
        in text
    )


def test_compose_cloudflared_has_active_connection_probe_surface():
    text = _compose_text()
    assert "--metrics 0.0.0.0:2000" in text
    assert "ipv4_address: ${JARVIS_CLOUDFLARED_IP:-10.137.241.254}" in text


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


def test_compose_config_accepts_a_derived_custom_subnet():
    """All pinned peers can move together under a setup-derived subnet."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")

    env = {
        **os.environ,
        "JARVIS_NET_SUBNET": "10.88.40.0/24",
        "JARVIS_NET_GATEWAY_IP": "10.88.40.1",
        "JARVIS_CADDY_IP": "10.88.40.251",
        "JARVIS_CADDY_LOCAL_IP": "10.88.40.252",
        "JARVIS_DASHBOARD_IP": "10.88.40.253",
        "JARVIS_CLOUDFLARED_IP": "10.88.40.254",
    }
    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
