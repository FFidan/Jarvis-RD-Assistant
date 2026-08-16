"""Semantic contracts for security- and recovery-sensitive public docs.

The access-mode table remains a generated parity boundary with
``scripts/setup_lib.sh``. The remaining tests deliberately verify concepts
inside their owning sections instead of snapshotting prose: installation,
identity, visibility, recovery, integration, and migration claims must stay
aligned with the executable configuration that ships with JARVIS.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_README = _REPO_ROOT / "README.md"
_SETUP_SCRIPT = _REPO_ROOT / "setup.sh"
_SETUP_LIB = _REPO_ROOT / "scripts" / "setup_lib.sh"
_ACCESS_MODES_DOC = _REPO_ROOT / "docs" / "manual" / "access-modes.md"
_GETTING_STARTED_DOC = _REPO_ROOT / "docs" / "manual" / "getting-started.md"
_DEPLOYMENT_DOC = _REPO_ROOT / "docs" / "DEPLOYMENT.md"
_REQUIREMENTS_DOC = _REPO_ROOT / "docs" / "REQUIREMENTS.md"
_SECURITY_DOC = _REPO_ROOT / "docs" / "SECURITY.md"
_ARCHITECTURE_DOC = _REPO_ROOT / "docs" / "ARCHITECTURE.md"
_PRD_DOC = _REPO_ROOT / "docs" / "PRD.md"
_KNOWN_RISKS_DOC = _REPO_ROOT / "docs" / "known-residual-risks.md"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_MIGRATIONS_DOC = _REPO_ROOT / "db" / "migrations" / "README.md"
_ADMIN_DOC = _REPO_ROOT / "docs" / "manual" / "admin.md"
_ASK_DOC = _REPO_ROOT / "docs" / "manual" / "ask.md"
_BACKUP_DOC = _REPO_ROOT / "docs" / "manual" / "backup-and-restore.md"
_PASSKEYS_DOC = _REPO_ROOT / "docs" / "manual" / "passkeys.md"
_RESEARCH_FEED_DOC = _REPO_ROOT / "docs" / "manual" / "research-feed.md"
_SETTINGS_DOC = _REPO_ROOT / "docs" / "manual" / "settings.md"
_HARDWARE_MODELS_DOC = _REPO_ROOT / "docs" / "manual" / "hardware-and-models.md"
_MODELS_CONTRACT_DOC = _REPO_ROOT / "docs" / "contracts" / "05-models-and-hardware.md"
_TELEGRAM_DOC = _REPO_ROOT / "docs" / "manual" / "telegram.md"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_JARVIS_SETUP_SCRIPT = _REPO_ROOT / "scripts" / "jarvis-setup.sh"
_LOCAL_CADDY_CONFIG = _REPO_ROOT / "caddy" / "Caddyfile.local"
_PASSKEY_ROUTER = (
    _REPO_ROOT / "services" / "platform_api" / "platform_api" / "routers" / "auth_passkeys.py"
)
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"
_NGINX_CONFIG = _REPO_ROOT / "frontend" / "nginx.conf"
_COMPOSE_CONFIG = _REPO_ROOT / "docker-compose.yml"
_CADDY_CONFIGS = (
    _REPO_ROOT / "caddy" / "Caddyfile",
    _REPO_ROOT / "caddy" / "Caddyfile.local",
)

_COLUMNS = (
    "route",
    "scheme",
    "port",
    "host_allowlist",
    "setup_token_transport",
    "cookie_policy",
    "passkey_origin",
    "cert_owner",
    "tier",
)

_BEGIN_MARKER = "<!-- route-claims:begin -->"
_END_MARKER = "<!-- route-claims:end -->"
_SETUP_TIERS = ("cpu", "lt-8", "8-16", "16-24", "24-48", "ge-48")


def _read(path: Path) -> str:
    """Return one UTF-8 documentation or configuration source."""
    return path.read_text(encoding="utf-8")


def _normalized_words(text: str) -> str:
    """Collapse formatting whitespace while preserving document wording.

    Parameters
    ----------
    text : str
        Markdown source or a selected Markdown section.

    Returns
    -------
    str
        Source words joined by single spaces, suitable for semantic phrase
        assertions that should not depend on line wrapping.
    """
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """Return a Markdown heading's body, bounded by the next peer heading.

    Parameters
    ----------
    text : str
        Complete Markdown source.
    heading : str
        Exact heading text without the leading ``#`` characters.

    Returns
    -------
    str
        The requested heading and its body through the next heading at the
        same or a higher level.

    Raises
    ------
    AssertionError
        If the requested heading is absent.
    """
    match = re.search(rf"^(?P<marks>#+) {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match is not None, f"missing Markdown section: {heading!r}"
    level = len(match.group("marks"))
    remainder = text[match.end() :]
    next_heading = re.search(rf"^#{{1,{level}}} ", remainder, re.MULTILINE)
    end = match.end() + (next_heading.start() if next_heading else len(remainder))
    return text[match.start() : end]


def _route_claims_rows() -> set[tuple[str, ...]]:
    """Run route_claims() via the real shell function and parse its pipe rows."""
    result = subprocess.run(
        ["bash", "-c", f"source {_SETUP_LIB}; route_claims"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    )
    rows: set[tuple[str, ...]] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        cols = tuple(c.strip() for c in line.split("|"))
        assert len(cols) == len(_COLUMNS), (
            f"route_claims row has {len(cols)} columns, expected {len(_COLUMNS)}: {line!r}"
        )
        rows.add(cols)
    return rows


def _doc_marker_block() -> str:
    text = _ACCESS_MODES_DOC.read_text(encoding="utf-8")
    begin = text.find(_BEGIN_MARKER)
    end = text.find(_END_MARKER)
    assert begin != -1 and end != -1 and end > begin, (
        "marker block not found: docs/manual/access-modes.md must contain a "
        f"{_BEGIN_MARKER} ... {_END_MARKER} delimited table"
    )
    return text[begin + len(_BEGIN_MARKER) : end]


def _doc_claims_rows() -> set[tuple[str, ...]]:
    """Parse the markdown table inside the marker block into column tuples.

    Skips the header row and the `|---|---|...` separator row; every
    remaining `| a | b | ... |` row is split into stripped cell values.
    """
    block = _doc_marker_block()
    rows: set[tuple[str, ...]] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0].lower() == _COLUMNS[0] or re.fullmatch(r"-+", cells[0]):
            continue  # header row or `---` separator row
        assert len(cells) == len(_COLUMNS), (
            f"docs route-claims row has {len(cells)} columns, expected {len(_COLUMNS)}: {line!r}"
        )
        rows.add(tuple(cells))
    return rows


def test_access_modes_route_claims_table_matches_setup_lib() -> None:
    """The marker-delimited table in access-modes.md must equal route_claims() exactly."""
    code_rows = _route_claims_rows()
    doc_rows = _doc_claims_rows()

    assert doc_rows, "docs route-claims table is empty — every route_claims() route must be listed"
    missing_from_docs = code_rows - doc_rows
    extra_in_docs = doc_rows - code_rows
    assert not missing_from_docs, (
        f"route_claims() rows missing from access-modes.md: {sorted(missing_from_docs)}"
    )
    assert not extra_in_docs, (
        f"access-modes.md claims routes/values route_claims() does not grant: "
        f"{sorted(extra_in_docs)}"
    )


def test_published_docs_match_the_two_listener_ingress_contract() -> None:
    """Published routes must agree with nginx, Compose, and the trusted edges."""
    access = _ACCESS_MODES_DOC.read_text(encoding="utf-8")
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    security = _SECURITY_DOC.read_text(encoding="utf-8")
    nginx = _NGINX_CONFIG.read_text(encoding="utf-8")
    compose = _COMPOSE_CONFIG.read_text(encoding="utf-8")

    assert '"3000:/health/jarvis" 1;' in nginx
    assert "if ($jarvis_request_allowed = 0) { return 403; }" in nginx
    assert "listen 3002;" in nginx
    assert "127.0.0.1:${DASHBOARD_TRUSTED_HOST_PORT:-3003}:3002" in compose
    assert "http://<lan-ip>:3001/health/jarvis" in access
    assert "all other remote requests return HTTP 403" in deployment
    assert "container `3002`" in security

    for caddy_path in _CADDY_CONFIGS:
        caddy = caddy_path.read_text(encoding="utf-8")
        assert "reverse_proxy http://dashboard:3002" in caddy

    assert "http://dashboard:3002" in deployment
    assert "http://dashboard:3000" not in deployment


def test_published_docs_match_automatic_subnet_and_tailscale_setup() -> None:
    """The operator copy must not revive pre-v1.2 manual setup instructions."""
    setup = _SETUP_SCRIPT.read_text(encoding="utf-8")
    setup_lib = _SETUP_LIB.read_text(encoding="utf-8")
    access = _ACCESS_MODES_DOC.read_text(encoding="utf-8")
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "network.prefixlen > 27" in setup_lib
    assert "edges = [network.broadcast_address - offset for offset in range(5, 0, -1)]" in setup_lib
    assert "IPv4 `/27` or larger" in deployment
    assert "Do not edit Compose or nginx" in env_example
    assert "requires updating TWO sets of hard-coded" not in env_example

    assert "tailscale_install_plan()" in setup_lib
    assert "install_tailscale_for_access()" in setup
    assert 'if [ "$INSTALL_PREREQS" -ne 1 ]' in setup
    assert "sudo tailscale up" in setup
    assert "Non-interactive installation requires" in access
    assert "`--install-prereqs`" in access
    assert "WSL without systemd" in deployment


def test_deployment_lists_every_required_host_prerequisite() -> None:
    """Fresh-install guidance must include setup's hard prerequisites."""
    setup = _SETUP_SCRIPT.read_text(encoding="utf-8")
    setup_lib = _SETUP_LIB.read_text(encoding="utf-8")
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    readme = _README.read_text(encoding="utf-8")
    requirements = _REQUIREMENTS_DOC.read_text(encoding="utf-8")
    prerequisite_guide = deployment.split("### Pre-flight check", 1)[1].split(
        "### Authentication", 1
    )[0]
    prerequisite_words = " ".join(prerequisite_guide.split())

    assert "command -v python3" in setup
    assert "python3) needs_python3=1" in setup_lib
    for prerequisite in (
        "Docker Engine",
        "Docker Compose v2.24.4 or newer",
        "`openssl`",
        "`curl`",
        "Python 3",
    ):
        assert prerequisite in prerequisite_guide
    assert "setup can install the missing packages on supported hosts" in prerequisite_words
    assert "stops with manual installation guidance" in prerequisite_words
    assert "Compose v2.24.4+" in readme
    assert "Python 3" in readme
    assert "`curl`" in readme
    assert "Docker Compose v2.24.4+" in requirements
    for prerequisite in ("Python 3", "`openssl`", "`curl`", "`git`"):
        assert prerequisite in requirements


def test_local_https_setup_is_automatic_and_explains_trust_boundaries() -> None:
    """Local HTTPS should be guided without promising trust on another OS."""
    setup = _SETUP_SCRIPT.read_text(encoding="utf-8")
    setup_lib = _SETUP_LIB.read_text(encoding="utf-8")
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    getting_started = _GETTING_STARTED_DOC.read_text(encoding="utf-8")
    deployment_words = " ".join(deployment.split())
    getting_started_words = " ".join(getting_started.split())

    assert "bash scripts/init-mkcert.sh" in setup
    assert "self-signed cert" not in setup
    assert "locally trusted certificate" in setup
    assert "mkcert libnss3-tools" in setup_lib
    assert "mkcert nss-tools" in setup_lib
    assert "brew install" in setup_lib and "mkcert" in setup_lib and "nss" in setup_lib
    assert "needs `make certs` first" not in deployment
    assert "run `make certs` first" not in deployment
    assert "Windows browser" in deployment
    assert "outside the VM" in deployment
    assert "does not cross that boundary" in deployment
    assert "prints an HTTP localhost finish link for the outside browser" in deployment_words
    assert "HTTP stays inside the encrypted SSH connection" in deployment_words
    assert "prints an HTTP localhost finish link for the outside browser" in getting_started_words
    assert "Do not change that address to `https`" in getting_started_words
    assert "advanced repair" in deployment


def test_local_https_docs_preserve_the_browser_port_through_caddy() -> None:
    """Local invite and passkey origins must retain localhost:3443."""
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    caddy = _LOCAL_CADDY_CONFIG.read_text(encoding="utf-8")
    setup = _SETUP_SCRIPT.read_text(encoding="utf-8")
    passkeys = _PASSKEY_ROUTER.read_text(encoding="utf-8")
    nginx = _NGINX_CONFIG.read_text(encoding="utf-8")

    assert "header_up Host {hostport}" in caddy
    local_row = next(
        line for line in deployment.splitlines() if "Local HTTPS (`caddy_local`)" in line
    )
    assert "Preserved as `localhost:3443`" in local_row
    assert "rewritten to `localhost`" not in local_row
    assert "local-https preserves localhost:3443" in setup
    assert "local Caddy preserves the browser Host and port" in passkeys
    assert "local Caddy preserves Host and port" in nginx


def test_published_no_smtp_copy_never_sends_operators_to_logs() -> None:
    """Raw bearer links are returned only to an administrator, never logged."""
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "logged to stdout" not in env_example
    assert "SMTP required" not in env_example
    assert "administrator" in env_example
    assert "manual" in env_example


def test_first_admin_and_api_key_copy_matches_the_working_operator_path() -> None:
    """Fresh installs must use the token link and describe API-key scope honestly."""
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    getting_started = _GETTING_STARTED_DOC.read_text(encoding="utf-8")
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    env_words = " ".join(env_example.split())

    assert "open the exact **Finish setup** link" in deployment
    assert "open the dashboard and create the initial admin" not in deployment
    assert "exact localhost finish link printed by setup" in getting_started
    assert "https://localhost:3443" in getting_started
    assert "authenticates ALL API requests" not in env_example
    assert "configured instance owner" in env_words
    assert "Family members use passkeys or one-time links" in env_words


def test_readme_names_one_full_noninteractive_installer() -> None:
    """The deprecated entry point must be documented and implemented as a forwarder."""
    readme = _README.read_text(encoding="utf-8")
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    legacy = _JARVIS_SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "./setup.sh --non-interactive" in readme
    assert "deprecated compatibility forwarder" in readme
    assert "deprecated compatibility forwarder" in deployment
    assert 'exec "${REPO_ROOT}/setup.sh" "${setup_args[@]}"' in legacy
    assert "docker compose" not in legacy


def test_published_docs_match_access_reconfiguration_and_cloudflare_trust() -> None:
    """Mode changes and Cloudflare client-IP trust must remain automatic and bounded."""
    setup = _SETUP_SCRIPT.read_text(encoding="utf-8")
    setup_lib = _SETUP_LIB.read_text(encoding="utf-8")
    nginx = _NGINX_CONFIG.read_text(encoding="utf-8")
    access = _ACCESS_MODES_DOC.read_text(encoding="utf-8")
    deployment = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    security = _SECURITY_DOC.read_text(encoding="utf-8")
    readme = _README.read_text(encoding="utf-8")
    dockerignore = _DOCKERIGNORE.read_text(encoding="utf-8")
    access_words = " ".join(access.split())

    assert "environment_for_access_route" in setup
    assert "environment_for_access_route()" in setup_lib
    assert 'read -rsp "Paste your tunnel token: "' in setup
    assert "_ACCESS_EDGE_RETIREMENTS=" in setup
    assert "rollback_access_runtime" in setup
    assert "begin_setup_transaction" in setup
    assert "recover_interrupted_setup_transaction" in setup
    assert "quiesce_previous_access_runtime" in setup
    assert "access_edge_retirements()" in setup_lib
    assert "rollback_access_runtime()" in setup_lib
    assert "tailscale_serve_https_off()" in setup_lib
    dockerignore_lines = set(dockerignore.splitlines())
    assert ".jarvis-setup-transaction" in dockerignore_lines
    assert ".jarvis-setup-transaction.pending" in dockerignore_lines
    assert "shared/" in dockerignore_lines

    assert "--overwrite-env" in access
    assert "accepts a replacement route only after it verifies" in access_words
    assert "not echoed" in access
    assert "from another LAN device" in access
    assert "exits nonzero until the address works" in access
    assert "restores and verifies the previous route" in access_words
    assert "prints exact manual commands" in access_words
    assert "operator procedure" in access_words
    assert "--overwrite-env" in deployment
    assert "last working dashboard" in deployment
    assert "Running setup again resumes" in deployment
    assert "retained recovery data" in deployment
    assert ".jarvis-setup-transaction/" not in deployment
    assert ".env.pre-setup.bak" not in deployment
    assert "does not install or\nconfigure that proxy" in deployment
    assert "exits\nnonzero until the address reaches this installation" in deployment
    for tunnel_flag in (
        "--profile=tunnel",
        "--tunnel-ack",
        "--tunnel-hostname",
        "--tunnel-token-file",
    ):
        assert tunnel_flag in deployment
    assert "Set `JARVIS_TRUST_CF_CONNECTING_IP" not in deployment
    assert "pinned `cloudflared`" in security
    assert "CF-Connecting-IP" in security
    for production_route in (
        "Tailscale",
        "Cloudflare Tunnel",
        "Let's Encrypt",
        "named `--public-origin`",
    ):
        assert production_route in readme
    assert "restores the last verified route" in readme

    for cloudflare_contract in (
        "jarvis.example.com/health/jarvis",
        "Bypass / Everyone",
        "Never bypass `/*`",
        "disables Access enforcement",
        "no account, setup-token, or health",
        "http://cloudflared:2000/ready",
    ):
        assert cloudflare_contract in deployment
    assert "your-hostname/health/jarvis" in access
    assert "Bypass / Everyone" in access
    assert "Never bypass the whole application" in setup

    assert "$jarvis_cf_ingress" in nginx
    assert "X-Jarvis-CF-Ingress" in nginx
    assert "$jarvis_cf_connecting_ip" in nginx


def test_install_examples_use_host_paths_and_cover_supported_access_profiles() -> None:
    """Unattended examples must be executable from the host for every access profile."""
    readme = _read(_README)
    deployment = _read(_DEPLOYMENT_DOC)
    install_docs = f"{readme}\n{deployment}"

    for profile in ("dev", "local-https", "tunnel", "letsencrypt"):
        assert f"--profile={profile}" in install_docs

    assert "--smtp-pass-file=./smtp-password.txt" in readme
    assert "--smtp-pass-file=./smtp-password.txt" in deployment
    assert "/run/secrets/smtp_pass" not in readme
    assert "/run/secrets/smtp_pass" not in deployment


def test_restore_guide_covers_complete_data_and_identity_recovery() -> None:
    """The canonical restore runbook must cover every durable recovery boundary."""
    backup = _read(_BACKUP_DOC)
    contents = _section(backup, "What a backup contains")
    identities = _section(backup, "Identity and credential boundaries")
    off_host = _normalized_words(_section(backup, "Recovering a fresh server in the browser"))

    assert "PDF archive" in contents
    assert "required" in contents.lower()
    assert "browser upload service" in off_host
    assert "backup sidecar" in off_host

    data_keys = set(
        re.findall(
            r"`((?:jarvis_config_key|jarvis_model_hmac_key|litellm_salt_key)\.txt)`",
            identities,
        )
    )
    assert data_keys == {
        "jarvis_config_key.txt",
        "jarvis_model_hmac_key.txt",
        "litellm_salt_key.txt",
    }
    assert "accounts, roles, and passkeys" in identities
    assert "sessions" in identities and "signed out" in identities
    assert "database-backed" in identities
    assert "target host" in identities
    assert "I HAVE REVIEWED RESTORED CREDENTIALS" in off_host
    assert "jarvis-research restore acknowledge <restore-id>" in off_host


def test_restore_guide_separates_data_recovery_from_application_recovery() -> None:
    """A data restore must not be presented as recovering missing application images."""
    backup = _read(_BACKUP_DOC)
    recovery = _section(backup, "Application recovery is separate")

    assert "data restore" in recovery.lower()
    assert "application images" in recovery
    assert "jarvis-research update" in recovery
    assert "does not" in recovery.lower()


def test_owner_upgrade_and_transfer_contract_is_documented() -> None:
    """Upgrade repair and in-product owner protections must agree with migration 0105."""
    admin = _read(_ADMIN_DOC)
    ownership = _section(admin, "Instance ownership and recovery")

    assert "0105" in ownership
    assert "exactly one live administrator" in ownership
    assert "two or more live administrators" in ownership
    assert "jarvis-research owner status" in ownership
    assert "jarvis-research owner set <admin-email>" in ownership
    assert "instance owner" in ownership
    assert "demoted" in ownership and "removed" in ownership
    assert "another live administrator" in ownership
    assert "email" in ownership


def test_passkey_recovery_requires_a_stable_hostname_and_rp_id() -> None:
    """Restore guidance must explain that WebAuthn credentials stay origin-bound."""
    passkeys = _read(_PASSKEYS_DOC)
    recovery = _section(passkeys, "After a restore or hostname change")

    assert "RP ID" in recovery
    assert "same hostname" in recovery
    assert "re-register" in recovery
    assert "APP_BASE_URL" in recovery


def test_smtp_docs_distinguish_database_settings_from_the_host_secret() -> None:
    """Operator copy must keep the two supported SMTP configuration layers distinct."""
    settings = _normalized_words(_section(_read(_SETTINGS_DOC), "Email / SMTP"))
    deployment = _read(_DEPLOYMENT_DOC)
    admin = _read(_ADMIN_DOC).lower()

    assert "deployment-wide" in settings
    assert "database" in settings
    assert "encrypted" in settings
    assert "without a service restart" in settings
    assert "Docker secret" in deployment
    assert "secrets/smtp_pass.txt" in deployment
    assert "without smtp" in admin
    assert "manual" in admin.lower()


def test_telegram_docs_cover_personal_pairing_profile_and_restart_limits() -> None:
    """Telegram setup must distinguish per-user pairing from server-level activation."""
    telegram = _read(_TELEGRAM_DOC)
    admin_token = _section(
        telegram, "Admin: configuring the bot token — Settings → Integrations → Bot Token"
    )

    assert "Each user pairs" in telegram
    assert "`telegram` Compose profile" in telegram
    assert "restart" in admin_token.lower()
    assert "does not start" in telegram
    assert "takes effect immediately" not in admin_token


def test_settings_guide_names_each_integration_scope() -> None:
    """Settings copy must identify who owns each credential or configuration."""
    settings = _normalized_words(_read(_SETTINGS_DOC))

    assert "Provider settings are deployment-wide" in settings
    assert "SMTP settings are deployment-wide" in settings
    assert "The bot token is deployment-wide" in settings
    assert "pairing is personal" in settings
    assert "Zotero credentials are per-user" in settings
    assert "Zotero-imported papers remain private" in settings


def test_restore_capability_table_matches_compose_mount_boundaries() -> None:
    """The operator guide must mirror the app, sidecar, and upload-service mounts."""
    deployment = _read(_DEPLOYMENT_DOC)
    compose = _read(_COMPOSE_CONFIG)
    boundaries = _section(deployment, "Restore capability boundaries")

    for service in ("paper_ingestion", "postgres-backup", "restore-uploader"):
        assert f"`{service}`" in boundaries
    assert "read-only" in boundaries
    assert "restore inbox" in boundaries
    assert "Docker socket" in boundaries

    assert "backup_trigger:/backup-trigger:ro" in compose
    assert "restore_inbox:/restore-inbox:rw" in compose
    assert "./secrets:/host-secrets:rw" in compose
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in compose


def test_security_copy_limits_owner_recovery_and_suppressed_email_logs() -> None:
    """Security docs must not turn recovery keys or log metadata into login paths."""
    security = _read(_SECURITY_DOC)
    threat_model = _section(security, "Three Identities")
    flags = _section(security, "Dev Flags and Production Refusal")

    assert "configured instance owner" in threat_model
    assert "cannot mint sessions for other users" in threat_model
    assert "SHA-256" in flags
    assert "recipient hash" in flags
    assert "bearer link" in flags
    assert "written to stdout/logs" not in flags


def test_hardware_and_disk_guidance_names_the_real_diagnostics_boundary() -> None:
    """Hardware and cleanup guidance must point to the UI and state host-wide impact."""
    admin = _section(_read(_ADMIN_DOC), "System Health — `/admin/system-health`")
    requirements = _section(_read(_REQUIREMENTS_DOC), "Disk budget")

    assert "model runtime diagnostics" in admin
    assert "Re-detect" in admin
    assert "Disk usage" in admin
    assert "optional" in requirements.lower()
    assert "host-wide" in requirements
    assert "docker builder prune -af" in requirements


def _leading_migration_version(path: Path) -> int | None:
    """Return the numeric prefix of a migration filename, or None if it has none."""
    try:
        return int(path.name.split("_")[0])
    except (ValueError, IndexError):
        return None


def _installer_default_contract() -> tuple[dict[str, tuple[str, str]], int, int]:
    """Execute the production selectors and disk calculator.

    Returns
    -------
    tuple[dict[str, tuple[str, str]], int, int]
        Per-tier smart model and complete pull set, followed by the smallest
        default pull requirement and largest default local-build requirement.
    """
    tiers = " ".join(_SETUP_TIERS)
    script = f"""
set -euo pipefail
source scripts/setup_lib.sh
for tier in {tiers}; do
  model="$(_default_model_for_tier "$tier" ollama)"
  printf 'tier|%s|%s|%s\\n' "$tier" "$model" "$(compute_ollama_models "$model")"
done
smallest="$(_default_model_for_tier cpu ollama)"
largest="$(_default_model_for_tier ge-48 ollama)"
printf 'bounds|%s|%s\\n' \
  "$(compute_required_disk_gb "$smallest" cpu-pull)" \
  "$(compute_required_disk_gb "$largest" cuda-build)"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    defaults: dict[str, tuple[str, str]] = {}
    minimum = maximum = 0
    for line in result.stdout.splitlines():
        kind, *fields = line.split("|")
        if kind == "tier":
            tier, model, model_set = fields
            defaults[tier] = (model, model_set)
        elif kind == "bounds":
            minimum, maximum = (int(value) for value in fields)

    assert tuple(defaults) == _SETUP_TIERS
    assert minimum > 0
    assert maximum >= minimum
    return defaults, minimum, maximum


def test_current_install_docs_follow_the_executable_model_and_disk_contract() -> None:
    """Current setup guidance must derive from production selectors."""
    defaults, minimum, maximum = _installer_default_contract()
    disk_range = f"{minimum}–{maximum} GB"

    readme = _read(_README)
    requirements = _read(_REQUIREMENTS_DOC)
    deployment = _read(_DEPLOYMENT_DOC)
    hardware = _read(_HARDWARE_MODELS_DOC)
    settings = _read(_SETTINGS_DOC)
    models_contract = _read(_MODELS_CONTRACT_DOC)
    env_example = _read(_ENV_EXAMPLE)
    setup = _read(_SETUP_SCRIPT)

    for current_doc in (readme, requirements, deployment, hardware, models_contract):
        assert disk_range in current_doc
    assert f"~{minimum}-{maximum} GB" in setup
    assert f"~{minimum}-{maximum} GB" in env_example

    for tier, (model, model_set) in defaults.items():
        assert tier in hardware
        assert model in hardware
        assert model_set in requirements

    assert "custom models may require more" in readme.lower()
    assert "custom models may require more" in requirements.lower()
    assert "manual template fallback" in env_example.lower()
    assert "tier-selected" in env_example.lower()
    assert "manual template fallback" in readme.lower()
    assert "tier-selected" in readme.lower()
    assert "tier-selected" in settings.lower()
    assert "tier-selected" in models_contract.lower()
    assert "default smart model is `qwen3:8b`" not in deployment
    assert "manual/hardware-and-models.md#hardware-tiers-and-default-models" in deployment

    assert "db/migrations/README.md" in readme
    assert re.search(r"0102\s*[–-]\s*\d{4}", readme) is None


def test_migration_ledger_names_baseline_and_every_post_baseline_step() -> None:
    """Migration docs must expose the exact schema floor and incremental history.

    The current version and the expected rows are read from ``db/SCHEMA_VERSION``
    and from the migration files themselves. Naming them literally here froze
    this guard at one release's numbers while the version file and the migration
    directory moved on, so it went green over exactly the drift it exists to
    catch. Deriving them also ties the version file to the highest migration on
    disk, which nothing else asserted.
    """
    migrations = _read(_MIGRATIONS_DOC)
    current = int((_REPO_ROOT / "db" / "SCHEMA_VERSION").read_text().strip())

    shipped = sorted(
        version
        for version in (
            _leading_migration_version(path)
            for path in (_REPO_ROOT / "db" / "migrations").glob("*.sql")
        )
        if version is not None
    )

    assert "baseline schema version is `101`" in migrations
    assert f"current schema version is `{current}`" in migrations, (
        f"the migration ledger must name the version in db/SCHEMA_VERSION ({current})"
    )
    assert shipped, "expected at least one post-baseline migration on disk"
    assert shipped[-1] == current, (
        f"db/SCHEMA_VERSION is {current} but the highest migration on disk is {shipped[-1]}"
    )
    for version in shipped:
        assert f"`{version:04d}`" in migrations, (
            f"migration {version:04d} ships but has no row in the ledger"
        )


def test_security_owns_one_source_aware_visibility_matrix_linked_by_consumers() -> None:
    """Public paper visibility must have one canonical matrix and linked summaries."""
    security = _read(_SECURITY_DOC)
    matrix = _normalized_words(_section(security, "Source-aware paper visibility"))

    for concept in (
        "Verified server source",
        "Local PDF upload",
        "Unverified client batch",
        "Personal or group Zotero",
        "Ambiguous legacy Zotero",
        "Unknown or unattributed",
    ):
        assert concept in matrix
    assert "private" in matrix.lower()
    assert "explicitly adds" in matrix
    assert "current visibility generation" in matrix

    expected_links = {
        _ADMIN_DOC: "../SECURITY.md#source-aware-paper-visibility",
        _RESEARCH_FEED_DOC: "../SECURITY.md#source-aware-paper-visibility",
        _ASK_DOC: "../SECURITY.md#source-aware-paper-visibility",
        _SETTINGS_DOC: "../SECURITY.md#source-aware-paper-visibility",
        _ARCHITECTURE_DOC: "SECURITY.md#source-aware-paper-visibility",
    }
    for path, link in expected_links.items():
        assert link in _read(path), f"{path.relative_to(_REPO_ROOT)} must link to {link}"


def test_current_docs_reject_legacy_global_corpus_and_qualify_history() -> None:
    """Current guidance must not inherit broader historical sharing language."""
    current_docs = "\n".join(
        _read(path)
        for path in (
            _ADMIN_DOC,
            _RESEARCH_FEED_DOC,
            _ASK_DOC,
            _SETTINGS_DOC,
            _ARCHITECTURE_DOC,
        )
    )
    changelog = _read(_CHANGELOG)

    assert "All discovered" not in current_docs
    assert "all papers any user" not in current_docs
    assert "shared corpus" not in current_docs.lower()
    assert "historical sharing language" in changelog.lower()
    assert "Source-aware paper visibility" in changelog


def test_product_truth_documents_the_shipped_passwordless_identity_model() -> None:
    """The PRD and architecture must name the current family-account auth paths."""
    prd = _read(_PRD_DOC)
    architecture = _read(_ARCHITECTURE_DOC)
    auth = _normalized_words(_section(prd, "3.5 Multi-Tenant Auth")).lower()

    for concept in (
        "setup token",
        "first administrator",
        "magic links",
        "passkeys",
        "sessions",
        "telegram pairing",
        "administrator",
        "manual",
        "without smtp",
        "family",
        "passwordless",
    ):
        assert concept in auth

    assert "user_sessions" not in prd
    assert "user_sessions" not in architecture
    assert "`sessions`" in architecture


def test_product_scope_excludes_collaboration_without_excluding_family_accounts() -> None:
    """Collaboration limits must not imply that isolated family accounts are absent."""
    prd = _read(_PRD_DOC)
    projects = _normalized_words(_section(prd, "3.3 Project Manager"))
    learning = _normalized_words(_section(prd, "3.2 Learning Engine"))

    assert "collaborative project" in projects
    assert "team workflow" in projects
    assert "per-user" in projects
    assert "family accounts" in projects
    assert "account isolation" in projects
    assert "collaborative decks" in learning


def test_prd_separates_shipped_zotero_and_retrieval_from_future_work() -> None:
    """Current capabilities must not remain mislabeled as roadmap-only work."""
    prd = _read(_PRD_DOC)
    zotero = _normalized_words(_section(prd, "3.4 Zotero Integration"))
    personalization = _normalized_words(
        _section(prd, "6.1 Personalization and citation signals (shipped)")
    )
    retrieval = _normalized_words(_section(prd, "6.3 Advanced retrieval"))

    assert "manual and scheduled Zotero" in zotero
    assert "personal or group library" in zotero
    assert "private" in zotero
    assert "classifier" in personalization
    assert "citation graph signals" in personalization
    assert "Missing Foundational Papers" in personalization
    assert "Cross-paper Ask" in retrieval
    assert "tracked-author discovery" in retrieval
    assert "multi-round synthesis" in retrieval.lower()
    assert "All core functionality accessible from Telegram" not in prd
