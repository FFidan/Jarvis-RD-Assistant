"""Regression tests for the pinned ingress trust boundary.

Verifies that:
  (a) nginx trusts only the parameterized host gateway and edge peers.
  (b) nginx.conf no longer contains the old broad-CIDR trust surface
      (172.16.0.0/12 or the NGINX_TRUSTED_PROXY_CIDR variable reference).
  (c) Compose parameterizes the gateway, Caddy, dashboard, and cloudflared IPs.
  (d) docker-compose.yml dashboard service no longer passes NGINX_TRUSTED_PROXY_CIDR.
  (e) docker-compose.yml parses as valid YAML and (if docker CLI is present)
      passes `docker compose config -q`.
  (f) every proxying location -- browser-reachable or internal subrequest --
      rebuilds the forwarding headers and strips browser-controlled identity,
      and the rendered configuration passes nginx's own syntax check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIR = REPO_ROOT / "frontend"
NGINX_CONF = FRONTEND_DIR / "nginx.conf"
NGINX_RATE_LIMIT_CONF = FRONTEND_DIR / "nginx-rate-limit.conf"
NGINX_SECURITY_HEADERS_CONF = FRONTEND_DIR / "nginx-security-headers.conf"
NGINX_IDENTITY_STRIP_CONF = FRONTEND_DIR / "nginx-identity-strip.conf"
NGINX_FORWARDED_HEADERS_CONF = FRONTEND_DIR / "nginx-forwarded-headers.conf"
FRONTEND_DOCKERFILE = FRONTEND_DIR / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
CADDY_LOCAL_FILE = REPO_ROOT / "caddy" / "Caddyfile.local"

# The forwarding facts nginx must derive from the listener boundary. Every value
# comes from a map keyed on $server_port/$realip_remote_addr, never from a
# request header, so a browser cannot choose the client address a backend sees.
FORWARDING_HEADER_DIRECTIVES = (
    "proxy_set_header X-Real-IP $jarvis_client_ip;",
    "proxy_set_header X-Forwarded-For $jarvis_client_ip;",
    "proxy_set_header X-Forwarded-Proto $jarvis_forwarded_proto;",
    "proxy_set_header X-Forwarded-Host $http_host;",
    'proxy_set_header Forwarded "";',
    "proxy_set_header CF-Connecting-IP $jarvis_cf_connecting_ip;",
    "proxy_set_header X-Jarvis-CF-Ingress $jarvis_cf_ingress;",
)

# Substitutions the nginx image's envsubst entrypoint applies to the template,
# mirroring the dashboard service environment in docker-compose.yml.
RENDER_ENVIRONMENT = {
    "DASHBOARD_SERVER_NAME": "",
    "DASHBOARD_BIND_HOST": "127.0.0.1",
    "JARVIS_NET_GATEWAY_IP": "10.137.241.1",
    "JARVIS_CADDY_IP": "10.137.241.251",
    "JARVIS_CADDY_LOCAL_IP": "10.137.241.252",
    "JARVIS_CLOUDFLARED_IP": "10.137.241.254",
}
# nginx resolves every proxy_pass host while testing a configuration, so the
# syntax check needs the backend service names to exist.
RENDER_UPSTREAM_HOSTS = ("platform_api", "paper_ingestion", "learning_engine", "restore-uploader")


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


# ``internal;`` is a standalone directive. Matching it as a bare substring also
# hits ``proxy_pass .../health/internal;``, which silently dropped two real
# browser-reachable proxies from every per-block assertion below.
_INTERNAL_DIRECTIVE = re.compile(r"^\s*internal;\s*$", flags=re.MULTILINE)
_LOCAL_INCLUDE = re.compile(r"^\s*include /etc/nginx/(nginx-[a-z-]+\.conf);\s*$")


def _proxy_blocks(text: str) -> list[str]:
    """Return the body of every location that proxies to a backend."""
    return [block for block in _location_blocks(text) if "proxy_pass" in block]


def _browser_proxy_blocks(text: str) -> list[str]:
    """Return externally reachable proxy locations.

    Parameters
    ----------
    text : str
        Renderable nginx configuration.

    Returns
    -------
    list[str]
        Proxy location bodies excluding nginx-only internal subrequests.
    """
    return [block for block in _proxy_blocks(text) if not _INTERNAL_DIRECTIVE.search(block)]


def _internal_proxy_blocks(text: str) -> list[str]:
    """Return the proxy locations reachable only as an nginx subrequest."""
    return [block for block in _proxy_blocks(text) if _INTERNAL_DIRECTIVE.search(block)]


def _expand_includes(text: str) -> str:
    """Return ``text`` with its repo-local nginx includes inlined recursively.

    Directives hoisted into a shared include are still emitted per location, so
    an assertion about a location's effective header set has to follow the
    include the same way nginx does.

    Parameters
    ----------
    text : str
        A location body or include file contents.

    Returns
    -------
    str
        The same text with every ``include /etc/nginx/nginx-*.conf;`` replaced
        by the contents of the matching file under ``frontend/``.
    """
    lines: list[str] = []
    for line in text.splitlines():
        match = _LOCAL_INCLUDE.match(line)
        if match:
            lines.append(_expand_includes((FRONTEND_DIR / match.group(1)).read_text()))
        else:
            lines.append(line)
    return "\n".join(lines)


@contextmanager
def _rendered_config_tree(rendered: str) -> Iterator[Path]:
    """Lay out the rendered dashboard configuration the way the image does.

    The destinations are read from ``frontend/Dockerfile`` rather than repeated
    here, so an include added to nginx.conf without a matching ``COPY`` fails
    the syntax check instead of only failing at container start.

    Parameters
    ----------
    rendered : str
        nginx.conf with the envsubst placeholders already resolved.

    Yields
    ------
    Path
        Directory holding a ``conf.d`` tree and an ``include`` directory whose
        files are mounted directly under ``/etc/nginx``.
    """
    copies = re.findall(
        r"^COPY (nginx-\S+\.conf) (/etc/nginx/\S+)$",
        FRONTEND_DOCKERFILE.read_text(),
        flags=re.MULTILINE,
    )
    assert copies, "frontend/Dockerfile copies no nginx include files"

    with tempfile.TemporaryDirectory() as name:
        tree = Path(name)
        (tree / "conf.d").mkdir()
        (tree / "include").mkdir()
        (tree / "conf.d" / "default.conf").write_text(rendered)
        for source, destination in copies:
            target = Path(destination)
            parent = "conf.d" if target.parent.name == "conf.d" else "include"
            shutil.copy(FRONTEND_DIR / source, tree / parent / target.name)
        yield tree


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
    forwarded = NGINX_FORWARDED_HEADERS_CONF.read_text()
    assert "$http_x_forwarded_proto" not in text + forwarded
    assert "proxy_set_header X-Forwarded-Proto $jarvis_forwarded_proto;" in forwarded
    assert "3002 https;" in text


def test_nginx_rebuilds_forwarding_headers_instead_of_appending_client_input():
    text = _nginx_text()
    forwarded = NGINX_FORWARDED_HEADERS_CONF.read_text()
    assert "real_ip_header X-Forwarded-For;" in text
    assert "$proxy_add_x_forwarded_for" not in text + forwarded
    assert "proxy_set_header X-Forwarded-For $jarvis_client_ip;" in forwarded
    assert 'proxy_set_header Forwarded "";' in forwarded
    assert "proxy_set_header X-Forwarded-Host $http_host;" in forwarded


def test_nginx_preserves_the_validated_external_port_for_generated_links():
    """localhost and other non-default origins need their Host port intact.

    Nginx's ``$host`` drops the port, producing manual invite links such as
    ``http://localhost/auth/verify`` for a dashboard actually served on :3001.
    ``$http_host`` retains the browser-facing port after the server_name
    allowlist has already validated the hostname.
    """
    for block in _browser_proxy_blocks(_nginx_text()):
        expanded = _expand_includes(block)
        assert "proxy_set_header Host $http_host;" in block
        assert "proxy_set_header X-Forwarded-Host $http_host;" in expanded
        assert "proxy_set_header Host $host;" not in expanded
        assert "proxy_set_header X-Forwarded-Host $host;" not in expanded


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
    for block in _browser_proxy_blocks(text):
        expanded = _expand_includes(block)
        assert "proxy_set_header CF-Connecting-IP $jarvis_cf_connecting_ip;" in expanded
        assert "proxy_set_header X-Jarvis-CF-Ingress $jarvis_cf_ingress;" in expanded


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
    assert "proxy_pass http://jarvis_research/health/live;" in text
    assert "location = /health/learning_engine/live" in text
    assert "proxy_pass http://jarvis_learning/health/live;" in text
    # The named upstreams exist so connections are reused; they must still
    # resolve to the backends themselves, or the liveness route proves nothing.
    assert "upstream jarvis_research {\n    server paper_ingestion:8000;" in text
    assert "upstream jarvis_learning {\n    server learning_engine:8001;" in text


def test_nginx_proxied_locations_strip_browser_supplied_identity():
    """Every proxied location clears the identity headers a browser can forge.

    The health, jobs, and recovery-upload routes used to pass the whole
    ``X-Jarvis-*`` family straight to their backend. The strip is a single
    include, so the assertion follows includes rather than accepting either a
    hoisted include or an inline copy -- the alternative that let those routes
    stay uncovered.
    """
    proxied_blocks = _proxy_blocks(_nginx_text())
    assert len(proxied_blocks) >= 1, "no proxy_pass location blocks found in nginx.conf"
    identity_strip = NGINX_IDENTITY_STRIP_CONF.read_text()
    stripped_headers = re.findall(r'proxy_set_header ([A-Za-z0-9-]+) "";', identity_strip)
    assert "X-Owner-User-Id" in stripped_headers
    for block in proxied_blocks:
        expanded = _expand_includes(block)
        missing = [
            header
            for header in stripped_headers
            if f'proxy_set_header {header} "";' not in expanded
        ]
        assert not missing, f"a proxied location forwards {missing} from the browser:\n{block}"


def test_nginx_forwarded_header_include_never_carries_host():
    """The hoisted group holds exactly the seven listener-derived facts.

    ``Host`` must stay per-location: the authorization subrequest pins it to the
    Platform service name while every other route forwards ``$http_host``. nginx
    emits repeated ``proxy_set_header`` directives for the same field instead of
    letting the later one win, so a ``Host`` line here would be sent in addition
    to -- not instead of -- the per-location value.
    """
    forwarded = NGINX_FORWARDED_HEADERS_CONF.read_text()
    directives = re.findall(r"^proxy_set_header .+;$", forwarded, flags=re.MULTILINE)
    assert directives == list(FORWARDING_HEADER_DIRECTIVES)
    assert not re.search(r"^proxy_set_header Host\b", forwarded, flags=re.MULTILINE)
    # The include is a build artifact: without the COPY the container cannot start.
    assert (
        "COPY nginx-forwarded-headers.conf /etc/nginx/nginx-forwarded-headers.conf"
        in FRONTEND_DOCKERFILE.read_text()
    )


def test_nginx_every_proxy_location_rebuilds_the_forwarding_group():
    """No proxied route may relay the browser's own forwarding headers."""
    proxy_blocks = _proxy_blocks(_nginx_text())
    assert len(proxy_blocks) >= 1, "no proxy_pass location blocks found in nginx.conf"
    for block in proxy_blocks:
        expanded = _expand_includes(block)
        missing = [
            directive for directive in FORWARDING_HEADER_DIRECTIVES if directive not in expanded
        ]
        assert not missing, f"a proxied location does not rebuild {missing}:\n{block}"


def test_nginx_internal_authorization_subrequest_rebuilds_the_forwarding_group():
    """The one endpoint that mints identity must not trust browser input.

    ``/internal/platform-authorize`` is the sole internal proxy. Platform binds
    the caller's address into rate limiting and audit from what this subrequest
    forwards, so relaying the browser's ``X-Forwarded-For`` or
    ``CF-Connecting-IP`` would let a client choose the address recorded against
    every assertion it obtains.
    """
    internal_blocks = _internal_proxy_blocks(_nginx_text())
    assert len(internal_blocks) == 1, "expected exactly one internal proxy location"
    authorize = internal_blocks[0]
    assert "proxy_pass http://jarvis_platform_authorize/internal/authorize;" in authorize

    expanded = _expand_includes(authorize)
    for directive in FORWARDING_HEADER_DIRECTIVES:
        assert directive in expanded, f"the authorization subrequest does not set {directive!r}"

    # The subrequest's own transport contract, which the shared group must not
    # disturb: Platform's host allowlist, and a body-less GET subrequest.
    assert "proxy_set_header Host platform_api;" in authorize
    assert "proxy_set_header Host $http_host;" not in expanded
    assert "proxy_pass_request_body off;" in authorize
    assert 'proxy_set_header Content-Length "";' in authorize


def test_nginx_rendered_config_passes_the_syntax_check():
    """The template that ships must still parse once envsubst has run.

    Every other assertion here matches text in a file the container renders at
    start-up. Only nginx itself can tell whether the rendered result -- includes
    resolved, upstreams present -- is a configuration it will load.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")

    runtime_image = re.search(
        r"^ARG NGINX_RUNTIME_IMAGE=(\S+)$", FRONTEND_DOCKERFILE.read_text(), flags=re.MULTILINE
    )
    assert runtime_image, "frontend/Dockerfile must pin the nginx runtime image"

    rendered = _nginx_text()
    for name, value in RENDER_ENVIRONMENT.items():
        rendered = rendered.replace(f"${{{name}}}", value)
    assert "${" not in rendered, "an envsubst placeholder is missing from RENDER_ENVIRONMENT"

    with _rendered_config_tree(rendered) as tree:
        command = ["docker", "run", "--rm"]
        for host in RENDER_UPSTREAM_HOSTS:
            command += ["--add-host", f"{host}:127.0.0.1"]
        command += ["-v", f"{tree / 'conf.d'}:/etc/nginx/conf.d:ro"]
        for include in sorted((tree / "include").iterdir()):
            command += ["-v", f"{include}:/etc/nginx/{include.name}:ro"]
        command += ["--entrypoint", "nginx", runtime_image.group(1), "-t"]
        result = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)

    assert result.returncode == 0, f"nginx -t rejected the rendered config:\n{result.stderr}"


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
