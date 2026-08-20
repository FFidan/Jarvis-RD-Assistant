"""
Test docker-compose.yml invariants.

Ensures critical services have proper secret mounts (e.g., langfuse tracing keys)
and that locally-built images carry explicit pull semantics.
"""

import json
import os
import posixpath
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
LANGFUSE_DOCKERFILE = REPO_ROOT / "langfuse" / "Dockerfile.langfuse"

# Every service that pairs an `image:` tag with a `build:` context (drift guard).
BUILT_SERVICES = {
    "platform_api",
    "paper_ingestion",
    "learning_engine",
    "telegram_bot",
    "dashboard",
    "restore-uploader",
    "langfuse",
}
# Published to GHCR and pulled by default; the `build:` block is kept only for the
# contributor / `--build-local` path.
PUBLISHED_SERVICES = {
    "platform_api",
    "paper_ingestion",
    "learning_engine",
    "telegram_bot",
    "dashboard",
    "restore-uploader",
}
# Unpublished, local-build only (the observability-profile langfuse fork).
LOCAL_BUILD_SERVICES = {"langfuse"}
GHCR_PREFIX = "ghcr.io/limitcycle-oss/"


@pytest.fixture(scope="module")
def compose():
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    return yaml.safe_load(compose_path.read_text())


def test_raw_compose_config_has_no_optional_acme_warnings() -> None:
    """Unset optional ACME values must be quiet without Makefile defaults."""
    env = os.environ.copy()
    env.pop("LETSENCRYPT_DOMAIN", None)
    env.pop("LETSENCRYPT_EMAIL", None)
    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "LETSENCRYPT_DOMAIN" not in result.stderr
    assert "LETSENCRYPT_EMAIL" not in result.stderr


def _bootstrap_command(compose: dict) -> str:
    command = compose["services"]["ollama-bootstrap"]["command"]
    assert isinstance(command, list) and len(command) == 1
    return command[0].replace("$$", "$")


def test_ollama_bootstrap_is_one_shot(compose: dict) -> None:
    """A successful model pull must not restart and pull the same models forever."""
    assert compose["services"]["ollama-bootstrap"]["restart"] == "no"


def _pull_stub(tmp_path: Path, body: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ollama = bin_dir / "ollama"
    ollama.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    ollama.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["OLLAMA_MODELS"] = "research-model"
    env["COUNT_FILE"] = str(tmp_path / "count")
    return env


def test_ollama_bootstrap_retries_a_transient_pull(compose, tmp_path: Path) -> None:
    env = _pull_stub(
        tmp_path,
        'count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\n'
        "count=$((count + 1))\n"
        'printf "%s" "$count" > "$COUNT_FILE"\n'
        '[ "$count" -gt 1 ]\n',
    )
    result = subprocess.run(
        ["/bin/sh", "-c", _bootstrap_command(compose)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "count").read_text(encoding="utf-8") == "2"


def test_ollama_bootstrap_fails_after_bounded_retries(compose, tmp_path: Path) -> None:
    env = _pull_stub(
        tmp_path,
        'count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\n'
        "count=$((count + 1))\n"
        'printf "%s" "$count" > "$COUNT_FILE"\n'
        "exit 1\n",
    )
    result = subprocess.run(
        ["/bin/sh", "-c", _bootstrap_command(compose)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "research-model" in result.stdout + result.stderr
    assert (tmp_path / "count").read_text(encoding="utf-8") == "3"


def test_setup_mode_reaches_every_application_service(compose):
    """The installer's single/multi choice must reach runtime settings.

    ``setup.sh`` persists JARVIS_SETUP_MODE in .env. If Compose omits it, every
    fresh family install silently falls back to ``single`` and the backend also
    skips multi-user security gates that depend on this value.
    """
    expected = "${JARVIS_SETUP_MODE:-single}"
    for service in ("paper_ingestion", "learning_engine", "telegram_bot"):
        assert compose["services"][service]["environment"].get("JARVIS_SETUP_MODE") == expected, (
            f"{service} does not receive the setup mode selected by setup.sh"
        )


def test_owner_override_reaches_only_the_resolver_service(compose):
    """The advanced host override is optional and never exposed stack-wide."""
    services = compose["services"]
    assert services["paper_ingestion"]["environment"].get("OWNER_USER_ID") == ("${OWNER_USER_ID:-}")
    for name, service in services.items():
        if name != "paper_ingestion":
            assert "OWNER_USER_ID" not in _env_keys(service), (
                f"{name} receives an owner override it does not consume"
            )


def test_ask_rate_limit_reaches_only_research(compose):
    """The documented benchmark override must retain a production-safe default."""
    services = compose["services"]
    assert services["paper_ingestion"]["environment"].get("ASK_RATE_LIMIT") == (
        "${ASK_RATE_LIMIT:-10/minute}"
    )
    for name, service in services.items():
        if name != "paper_ingestion":
            assert "ASK_RATE_LIMIT" not in _env_keys(service)


def test_dashboard_has_no_tls_material_or_generator(compose):
    """TLS belongs to an edge service, never to the plain-HTTP dashboard.

    Keep the recovery capability split explicit while removing the dead
    dashboard certificate path: the application may request work, the lifecycle
    sidecar owns destructive restore mounts, and the upload ingress can write
    only its staging inbox.
    """
    services = compose["services"]
    dashboard = services["dashboard"]
    dashboard_env = _env_keys(dashboard)
    assert {"JARVIS_CERT_SAN", "JARVIS_SKIP_SELFSIGNED_GEN"}.isdisjoint(dashboard_env)
    assert all("/etc/nginx/certs" not in str(mount) for mount in dashboard.get("volumes", []))

    cert_consumers = {
        name: mount
        for name, service in services.items()
        for mount in service.get("volumes", []) or []
        if isinstance(mount, str) and mount.startswith("./certs:")
    }
    assert cert_consumers == {"caddy_local": "./certs:/certs:ro"}
    assert {"caddy_data:/data", "caddy_config:/config"}.issubset(set(services["caddy"]["volumes"]))

    app_mounts = set(services["paper_ingestion"]["volumes"])
    assert "postgres_backups:/backups:ro" in app_mounts
    assert "backup_trigger:/backup-trigger" in app_mounts
    assert all(
        forbidden not in mount
        for mount in app_mounts
        for forbidden in ("/host-secrets", "/postgres-data", "/restore-inbox")
    )

    backup_mounts = set(services["postgres-backup"]["volumes"])
    assert {
        "./secrets/jarvis_config_key.txt:/data-keys/jarvis_config_key.txt:ro",
        "./secrets/jarvis_model_hmac_key.txt:/data-keys/jarvis_model_hmac_key.txt:ro",
        "./secrets/litellm_salt_key.txt:/data-keys/litellm_salt_key.txt:ro",
        "backup_state:/backup-state:rw",
    }.issubset(backup_mounts)
    assert all(not mount.startswith("./secrets:") for mount in backup_mounts)
    assert all("${JARVIS_STATE_DIR:-./secrets}" not in mount for mount in backup_mounts)
    assert "./secrets:/host-secrets:rw" not in backup_mounts
    assert "postgres_data:/postgres-data:ro" not in backup_mounts
    assert "restore_inbox:/restore-inbox" in backup_mounts

    restore_mounts = set(services["postgres-restore"]["volumes"])
    assert "./secrets:/host-secrets:rw" in restore_mounts
    assert "postgres_data:/postgres-data:ro" in restore_mounts
    assert "restore_inbox:/restore-inbox" in restore_mounts

    uploader = services["restore-uploader"]
    assert uploader.get("read_only") is True
    assert uploader.get("cap_drop") == ["ALL"]
    assert set(uploader["volumes"]) == {
        "restore_inbox:/restore-inbox:rw",
        "backup_trigger:/backup-trigger:ro",
    }

    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text()
    assert "openssl" not in dockerfile.lower()
    assert "generate-certs" not in dockerfile
    assert "USER root" not in dockerfile
    assert re.search(r"^USER nginx$", dockerfile, re.MULTILINE)
    assert not (REPO_ROOT / "frontend" / "scripts" / "generate-certs.sh").exists()


def test_dashboard_builder_pin_matches_every_build_entrypoint(compose):
    """The tested Node builder must reach direct and Compose builds unchanged."""
    versions = dict(
        line.split("=", 1)
        for line in (REPO_ROOT / "versions.env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    node_image = versions["NODE_BUILD_IMAGE"]
    dockerfile = (REPO_ROOT / "frontend" / "Dockerfile").read_text()
    compose_arg = compose["services"]["dashboard"]["build"]["args"]["NODE_BUILD_IMAGE"]

    assert re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", node_image), node_image
    assert f"ARG NODE_BUILD_IMAGE={node_image}" in dockerfile
    assert compose_arg == f"${{NODE_BUILD_IMAGE:-{node_image}}}"


def test_service_images_create_the_runtime_user_non_interactively() -> None:
    for dockerfile_path in (
        "services/paper_ingestion/Dockerfile",
        "services/learning_engine/Dockerfile",
        "services/telegram_bot/Dockerfile",
    ):
        dockerfile = (REPO_ROOT / dockerfile_path).read_text()
        assert 'adduser --disabled-password --no-create-home --gecos "" appuser' in dockerfile


def test_application_healthchecks_use_unconditional_liveness(compose):
    """Container restarts must not depend on deep downstream readiness."""
    expected = {
        "platform_api": "http://localhost:8003/health/live",
        "paper_ingestion": "http://localhost:8000/health/live",
        "learning_engine": "http://localhost:8001/health/live",
    }
    for service_name, endpoint in expected.items():
        probe = " ".join(compose["services"][service_name]["healthcheck"]["test"])
        assert endpoint in probe
        assert endpoint.removesuffix("/live") + "'" not in probe


def test_backup_lifecycle_mutex_volume_is_writable_only_by_recovery_workers(compose):
    """Applications may request work, but cannot replace lifecycle mutex inodes."""
    backup_mounts: dict[str, str] = {}
    for service_name, service in compose["services"].items():
        for mount in service.get("volumes", []):
            if isinstance(mount, str) and mount.startswith("postgres_backups:/backups"):
                backup_mounts[service_name] = mount

    assert backup_mounts.get("postgres-backup") == "postgres_backups:/backups"
    assert backup_mounts.get("paper_ingestion") == "postgres_backups:/backups:ro"
    assert all(
        service_name in {"postgres-backup", "postgres-restore"} or mount.endswith(":ro")
        for service_name, mount in backup_mounts.items()
    ), f"only backup or restore workers may write lifecycle mutexes under /backups: {backup_mounts}"


def test_backup_sidecar_mounts_the_live_pdf_store_for_backup_and_restore(compose):
    mounts = compose["services"]["postgres-backup"]["volumes"]
    assert "./shared/pdf_storage:/pdf-storage:rw" in mounts


def test_litellm_uses_the_restore_aware_entrypoint(compose):
    litellm = compose["services"]["litellm"]
    assert "./scripts/litellm-entrypoint.sh:/usr/local/bin/litellm-entrypoint.sh:ro" in set(
        litellm["volumes"]
    )
    assert litellm["entrypoint"] == ["sh", "/usr/local/bin/litellm-entrypoint.sh"]
    assert litellm["command"] == []
    assert litellm["environment"]["LITELLM_DB_CONNECTION_LIMIT"] == (
        "${LITELLM_DB_CONNECTION_LIMIT:-5}"
    )
    assert litellm["healthcheck"]["test"] == [
        "CMD",
        "sh",
        "/usr/local/bin/litellm-entrypoint.sh",
        "--healthcheck",
    ]


def test_langfuse_wrapper_preserves_the_pinned_server_command() -> None:
    """Replacing the upstream entrypoint must retain its default server command."""
    source = LANGFUSE_DOCKERFILE.read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["/usr/local/bin/langfuse-secrets-entrypoint.sh"]' in source
    assert 'CMD ["node", "./web/server.js", "--keepAliveTimeout", "110000"]' in source


def test_telegram_uses_only_scoped_platform_and_bot_credentials(compose):
    telegram = compose["services"]["telegram_bot"]
    environment = telegram["environment"]
    secret_names = {
        entry if isinstance(entry, str) else entry.get("source") for entry in telegram["secrets"]
    }
    assert environment["JARVIS_TELEGRAM_SERVICE_TOKEN_FILE"] == (
        "/run/secrets/telegram_service_token"
    )
    assert environment["PLATFORM_API_URL"] == "http://platform_api:8003"
    assert environment["PAPER_INGESTION_URL"] == "http://paper_ingestion:8000"
    assert environment["LEARNING_ENGINE_URL"] == "http://learning_engine:8001"
    assert secret_names == {"telegram_bot_token", "telegram_service_token"}
    assert "postgres" not in telegram["depends_on"]
    for forbidden in (
        "JARVIS_API_KEY_FILE",
        "JARVIS_CONFIG_KEY_FILE",
        "JARVIS_MODEL_HMAC_KEY_FILE",
        "LITELLM_MASTER_KEY_FILE",
    ):
        assert forbidden not in environment


def test_research_does_not_mount_platform_owned_telegram_credentials(
    compose: dict[str, Any],
) -> None:
    """Research must not receive the Platform-owned Telegram bot credential."""
    research = compose["services"]["paper_ingestion"]
    environment = research.get("environment", {}) or {}
    secret_names = {
        entry if isinstance(entry, str) else entry.get("source")
        for entry in research.get("secrets", [])
    }

    assert "TELEGRAM_BOT_TOKEN_FILE" not in environment
    assert "telegram_bot_token" not in secret_names


def test_every_secret_file_environment_path_is_mounted(compose: dict[str, Any]) -> None:
    """Every ``*_FILE`` path must resolve to a secret mounted by that service.

    Parameters
    ----------
    compose : dict[str, Any]
        Parsed Compose document.
    """
    for service_name, service in compose["services"].items():
        mounted_paths = {
            f"/run/secrets/{entry}"
            if isinstance(entry, str)
            else f"/run/secrets/{entry.get('target') or entry.get('source')}"
            for entry in service.get("secrets", [])
        }
        environment = service.get("environment", {}) or {}
        environment_items = (
            ((entry.partition("=")[0], entry.partition("=")[2]) for entry in environment)
            if isinstance(environment, list)
            else environment.items()
        )
        for variable, path in environment_items:
            if variable.endswith("_FILE") and isinstance(path, str):
                assert path in mounted_paths, (
                    f"{service_name}.{variable} points to {path!r}, but the service "
                    f"mounts only {sorted(mounted_paths)}"
                )


def test_langfuse_service_secrets_mounted_and_not_in_env(compose):
    """Invariant: langfuse service must mount the 3 secrets at
    /run/secrets/ AND must NOT carry DATABASE_URL/NEXTAUTH_SECRET/SALT as
    plaintext env vars (which would be visible via `docker inspect`).
    """
    langfuse = compose["services"]["langfuse"]

    mounted = {s if isinstance(s, str) else s.get("source") for s in langfuse.get("secrets", [])}
    required = {"langfuse_pg_password", "langfuse_nextauth_secret", "langfuse_salt"}
    assert required.issubset(mounted), f"langfuse service must mount {required}; got {mounted}"

    env = langfuse.get("environment", {}) or {}
    forbidden = {"DATABASE_URL", "NEXTAUTH_SECRET", "SALT"}
    leaked = forbidden & set(env.keys())
    assert not leaked, (
        f"Regression: {leaked} re-introduced as plaintext env vars "
        f"on langfuse service — `docker inspect` would expose them."
    )


def test_built_services_declare_explicit_pull_policy(compose):
    """Invariant: every service pairing an `image:` tag with a `build:` context
    must declare an explicit `pull_policy`. Published services install from GHCR
    by default (`pull_policy: missing` + a `ghcr.io/limitcycle-oss/` image), keeping
    their `build:` block only for the contributor `--build-local` path; the
    unpublished langfuse fork stays `pull_policy: build` (an implicit registry pull
    of its `jarvis/*` tag could only fail with "pull access denied").
    """
    built = {
        name: svc for name, svc in compose["services"].items() if "image" in svc and "build" in svc
    }
    assert set(built) == BUILT_SERVICES, (
        f"image+build service set changed: {set(built) ^ BUILT_SERVICES} — "
        "update BUILT_SERVICES/PUBLISHED_SERVICES and give any new service an explicit pull_policy"
    )
    assert PUBLISHED_SERVICES | LOCAL_BUILD_SERVICES == BUILT_SERVICES, (
        "PUBLISHED_SERVICES and LOCAL_BUILD_SERVICES must partition BUILT_SERVICES"
    )
    for name in PUBLISHED_SERVICES:
        svc = built[name]
        assert svc.get("pull_policy") == "missing", (
            f"{name} is published — must declare pull_policy: missing; "
            f"got {svc.get('pull_policy')!r}"
        )
        assert svc["image"].startswith(GHCR_PREFIX), (
            f"{name} is published — image must start with {GHCR_PREFIX!r}; got {svc['image']!r}"
        )
    for name in LOCAL_BUILD_SERVICES:
        svc = built[name]
        assert svc.get("pull_policy") == "build", (
            f"{name} is local-build only — must declare pull_policy: build; "
            f"got {svc.get('pull_policy')!r}"
        )


def test_paper_ingestion_image_selects_the_torch_variant(compose):
    """Invariant: the CUDA/CPU flavour is chosen solely by the image tag suffix.

    If `${TORCH_VARIANT_SUFFIX}` is ever dropped from paper_ingestion's tag, a CUDA
    host silently resolves the CPU image: no error, just a GPU-reserved container
    running CPU torch. Nothing else in the stack would catch that. The other
    published images are single-flavour and must NOT carry the suffix.
    """
    image = compose["services"]["paper_ingestion"]["image"]
    assert "${TORCH_VARIANT_SUFFIX" in image, (
        "paper_ingestion's image tag must interpolate ${TORCH_VARIANT_SUFFIX} — without it "
        f"a CUDA host silently runs the CPU image; got {image!r}"
    )
    for name in PUBLISHED_SERVICES - {"paper_ingestion"}:
        other = compose["services"][name]["image"]
        assert "TORCH_VARIANT_SUFFIX" not in other, (
            f"{name} is single-flavour — it has no -cuda image to select; got {other!r}"
        )


def test_jarvis_version_defaults_agree():
    """Invariant: every ``${JARVIS_VERSION:-<x>}`` default in docker-compose.yml is identical.

    JARVIS_VERSION drives both the published image tags AND the version the
    containers report: app_version() falls back to this env var in production
    (the images don't install the root distribution) and backup.sh stamps it
    into every backup manifest. With the var unset — the default install — a
    drifted default means pulling release-N images that report release-M via
    /health, the UI, and every backup manifest.
    """
    raw = (REPO_ROOT / "docker-compose.yml").read_text()

    # Every reference must CARRY a default: a bare ${JARVIS_VERSION} would resolve to an
    # empty string on a default install — an image tag of `jarvis-dashboard:` — so the
    # defaults being equal is not on its own enough.
    bare = re.findall(r"\$\{JARVIS_VERSION\}", raw)
    assert not bare, (
        f"{len(bare)} bare ${{JARVIS_VERSION}} reference(s) in docker-compose.yml — with the "
        "var unset these resolve to an empty tag; give each one a `:-<version>` default"
    )

    defaults = re.findall(r"\$\{JARVIS_VERSION:-([^}]*)\}", raw)
    assert len(defaults) >= 2, "expected multiple JARVIS_VERSION defaults in docker-compose.yml"
    assert len(set(defaults)) == 1, (
        f"JARVIS_VERSION defaults drifted apart: {sorted(set(defaults))} — the pulled image "
        "tags and the version the containers report/stamp into backups must agree"
    )

    # ...and they must agree with the release version itself, so the images cannot be
    # tagged for one release while the project declares another.
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert declared, "pyproject.toml must declare a version"
    assert defaults[0] == declared.group(1), (
        f"docker-compose.yml defaults JARVIS_VERSION to {defaults[0]!r} but pyproject.toml "
        f"declares {declared.group(1)!r} — the image tags and the release version must agree"
    )


def test_app_version_sources_agree():
    """Every application-version source must move in one release change."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text())
    package_lock = json.loads((REPO_ROOT / "frontend" / "package-lock.json").read_text())
    uv_lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    # CITATION.cff is what GitHub renders in the repository sidebar and what
    # Zenodo and other CFF consumers read, so a stale value there is published
    # metadata. Matched by line rather than parsed to avoid a YAML dependency.
    citation = re.search(
        r"^version:\s*(\S+)\s*$",
        (REPO_ROOT / "CITATION.cff").read_text(),
        re.MULTILINE,
    )
    assert citation, "CITATION.cff must declare a version"
    compose_defaults = re.findall(
        r"\$\{JARVIS_VERSION:-([^}]*)\}",
        (REPO_ROOT / "docker-compose.yml").read_text(),
    )

    assert len(compose_defaults) == 12, (
        "docker-compose.yml must carry exactly twelve application-version defaults; "
        f"found {len(compose_defaults)}"
    )
    root_packages = [
        entry
        for entry in uv_lock["package"]
        if entry["name"] == pyproject["project"]["name"] and entry.get("source") == {"virtual": "."}
    ]
    assert len(root_packages) == 1, "uv.lock must contain one virtual root-project entry"

    versions = {
        "pyproject.toml": pyproject["project"]["version"],
        "frontend/package.json": package["version"],
        "frontend/package-lock.json": package_lock["version"],
        "frontend/package-lock.json packages['']": package_lock["packages"][""]["version"],
        "uv.lock": root_packages[0]["version"],
        "CITATION.cff": citation.group(1),
    }
    for index, version in enumerate(compose_defaults, start=1):
        versions[f"docker-compose.yml default {index}"] = version

    assert len(set(versions.values())) == 1, f"application version sources disagree: {versions}"


def _bash_array_items(text: str, name: str) -> set[str]:
    """Collect every element assigned to a bash array (both `X=(...)` and `X+=(...)`)."""
    items: set[str] = set()
    for match in re.finditer(rf"^\s*{re.escape(name)}\+?=\(([^)]*)\)", text, re.MULTILINE):
        items |= {tok for tok in match.group(1).split() if tok}
    return items


def test_installer_scripts_pull_every_published_service(compose):
    """Invariant: the service lists the installers pull match compose's published set.

    setup.sh and update.sh pull the published images BY NAME. If a new published
    service is added to docker-compose.yml but not to those lists it is never pulled,
    and because every published service pairs `pull_policy: missing` with a `build:`
    block, `up` would SILENTLY BUILD it — the multi-GB torch build (and the ENOSPC)
    this release exists to eliminate, reappearing through a list that quietly fell out
    of sync. This test is that guard.
    """
    published_in_compose = {
        name
        for name, svc in compose["services"].items()
        if "build" in svc and str(svc.get("image", "")).startswith(GHCR_PREFIX)
    }
    assert published_in_compose == PUBLISHED_SERVICES, (
        "compose's published set drifted from PUBLISHED_SERVICES: "
        f"{published_in_compose ^ PUBLISHED_SERVICES}"
    )

    lib = (REPO_ROOT / "scripts" / "setup_lib.sh").read_text()
    telegram = re.search(r"^\s*PUBLISHED_SERVICE_TELEGRAM=(\S+)", lib, re.MULTILINE)
    assert telegram, "scripts/setup_lib.sh must declare PUBLISHED_SERVICE_TELEGRAM"
    declared = _bash_array_items(lib, "PUBLISHED_SERVICES_BASE") | {telegram.group(1)}
    assert declared == PUBLISHED_SERVICES, (
        "the shared published-service list does not match compose's published set; a service "
        f"missing from it is never pulled and so gets silently BUILT. Difference: "
        f"{declared ^ PUBLISHED_SERVICES}"
    )

    # Every entry point that brings the stack up must materialise those images from the
    # shared list rather than keeping its own copy — a hand-maintained second list is
    # exactly how a service silently falls back to being built.
    for script in ("setup.sh", "update.sh"):
        text = (REPO_ROOT / script).read_text()
        assert "PUBLISHED_SERVICES_BASE" in text, (
            f"{script} starts the stack but does not pull the shared published set — "
            "any image it leaves missing would be silently BUILT by `up`"
        )

    forwarder = (REPO_ROOT / "scripts" / "jarvis-setup.sh").read_text()
    assert 'exec "${REPO_ROOT}/setup.sh" "${setup_args[@]}"' in forwarder
    assert "PUBLISHED_SERVICES_BASE" not in forwarder


def _env_keys(svc) -> set[str]:
    """Environment keys of a service, handling both the mapping and list forms."""
    env = svc.get("environment", {}) or {}
    if isinstance(env, dict):
        return set(env.keys())
    return {str(e).split("=", 1)[0] for e in env}


def test_smtp_password_is_a_mounted_secret_never_plain_env(compose):
    """Invariant: the SMTP password reaches paper_ingestion as a Docker secret
    (SMTP_PASS_FILE -> /run/secrets/smtp_pass), never a plaintext SMTP_PASS env
    var that ``docker inspect`` would expose. The rest of the SMTP settings are
    env-configurable so an operator relay actually reaches the container — before
    this the whole SMTP_* set was absent and an env-configured relay was dead.
    """
    assert "smtp_pass" in (compose.get("secrets") or {}), (
        "top-level smtp_pass secret must be declared"
    )

    pi = compose["services"]["paper_ingestion"]
    mounted = {s if isinstance(s, str) else s.get("source") for s in pi.get("secrets", [])}
    assert "smtp_pass" in mounted, "paper_ingestion must mount the smtp_pass secret"

    env = pi.get("environment", {}) or {}
    assert env.get("SMTP_PASS_FILE") == "/run/secrets/smtp_pass", (
        f"paper_ingestion SMTP_PASS_FILE must point at the mounted secret; "
        f"got {env.get('SMTP_PASS_FILE')!r}"
    )
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_FROM"):
        assert key in env, (
            f"paper_ingestion must pass {key} so an env-configured relay reaches the app"
        )

    for name, svc in compose["services"].items():
        assert "SMTP_PASS" not in _env_keys(svc), (
            f"{name} exposes SMTP_PASS as a plaintext env var — use the smtp_pass Docker secret"
        )


GROUP_ADD_OK = re.compile(r"^(\d+|\$\{[A-Z0-9_]+:-\d+\})$")


@pytest.mark.parametrize("overlay", ["docker-compose.vulkan.yml", "docker-compose.rocm.yml"])
def test_overlay_group_add_entries_are_numeric(overlay):
    """group_add NAMES resolve against the container image's /etc/group and
    fail container start when absent (stock ollama images ship no `render`
    group). Numeric GIDs and ${VAR:-numeric} interpolations apply without
    any lookup."""
    data = yaml.safe_load((REPO_ROOT / overlay).read_text())
    for name, svc in data.get("services", {}).items():
        for entry in svc.get("group_add", []):
            assert GROUP_ADD_OK.match(str(entry)), (
                f"{overlay}:{name} group_add entry {entry!r} is a bare "
                "group name; use a numeric GID or ${VAR:-numeric}"
            )


def _resolved_host_port(entry) -> int:
    """Resolve a compose `ports:` entry to its published HOST port.

    Handles the short-form strings used here — ``ip:hostport:container`` and
    ``hostport:container`` — resolving ``${VAR:-default}`` to its default first
    (the colon inside a default would otherwise break a naive split).
    """
    if isinstance(entry, dict):  # long form {published: ..., target: ...}
        return int(str(entry["published"]))
    resolved = re.sub(r"\$\{[^}]*:-([^}]*)\}", r"\1", str(entry))
    return int(resolved.split(":")[-2])


def test_no_two_services_publish_the_same_host_port(compose):
    """Invariant: no two services publish the same host port.

    caddy_local (the local-https TLS terminator) and the dashboard both once
    published host 3001, so `make up-https` — which brings up BOTH — could never
    bind them together. Every published host port must be unique across services so
    any enabled profile combination starts cleanly.
    """
    owners: dict[int, list[str]] = {}
    for name, svc in compose["services"].items():
        for entry in svc.get("ports", []) or []:
            owners.setdefault(_resolved_host_port(entry), []).append(name)
    collisions = {port: names for port, names in owners.items() if len(names) > 1}
    assert not collisions, (
        f"services publish colliding host ports (they cannot bind together): {collisions}"
    )


def _brace_block(text: str, opener: str) -> str:
    """Return the body of the first brace-delimited block whose header matches ``opener``."""
    match = re.search(opener + r"[^{]*\{", text)
    assert match, f"no block matching {opener!r}"
    depth, pos = 1, match.end()
    while depth:
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
        pos += 1
    return text[match.end() : pos - 1]


def test_setup_status_ingress_routes_only_to_platform() -> None:
    """The public setup-status URL must terminate at the owning Platform API."""
    nginx = (REPO_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    block = _brace_block(nginx, r"location = /api/system/setup-status")

    assert "proxy_pass http://platform_api:8003;" in block
    assert "paper_ingestion" not in block
    assert "include /etc/nginx/nginx-identity-strip.conf;" in block


def test_operator_ingress_routes_only_to_platform() -> None:
    """Stable configuration and provider URLs terminate at their Platform owner."""
    nginx = (REPO_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    opener = re.escape(
        "location ~ ^/api/(auth|setup|admin|telegram|account|config|logs|providers)(/|$)"
    )
    block = _brace_block(nginx, opener)

    assert "proxy_pass http://platform_api:8003;" in block
    assert "paper_ingestion" not in block
    assert "nginx-backend-auth.conf" not in block
    assert "include /etc/nginx/nginx-identity-strip.conf;" in block


def test_gateway_strips_forged_identity_and_uses_only_platform_assertion() -> None:
    """Backend proxying must replace every browser-controlled identity field."""
    stripped = (REPO_ROOT / "frontend" / "nginx-identity-strip.conf").read_text(encoding="utf-8")
    expected_headers = {
        "X-Jarvis-Identity",
        "X-Jarvis-Principal",
        "X-Jarvis-Scopes",
        "X-Jarvis-Session-Id",
        "X-Jarvis-User-Id",
        "X-Jarvis-User-Role",
        "X-Owner-User-Id",
    }
    assert {
        match.group(1) for match in re.finditer(r"proxy_set_header ([A-Za-z0-9-]+) \"\";", stripped)
    } == expected_headers

    backend_auth = (REPO_ROOT / "frontend" / "nginx-backend-auth.conf").read_text(encoding="utf-8")
    assert "auth_request /internal/platform-authorize;" in backend_auth
    assert "include /etc/nginx/nginx-identity-strip.conf;" in backend_auth
    assert "proxy_set_header X-Jarvis-Identity $jarvis_identity;" in backend_auth
    assert 'proxy_set_header Connection "";' in backend_auth

    nginx = (REPO_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    upstream = _brace_block(nginx, r"upstream jarvis_platform_authorize")
    assert "server platform_api:8003;" in upstream
    assert "keepalive 32;" in upstream
    research_upstream = _brace_block(nginx, r"upstream jarvis_research")
    assert "server paper_ingestion:8000;" in research_upstream
    assert "keepalive 32;" in research_upstream
    learning_upstream = _brace_block(nginx, r"upstream jarvis_learning")
    assert "server learning_engine:8001;" in learning_upstream
    assert "keepalive 32;" in learning_upstream
    authorize = _brace_block(nginx, r"location = /internal/platform-authorize")
    assert "internal;" in authorize
    assert "proxy_pass http://jarvis_platform_authorize/internal/authorize;" in authorize
    assert "proxy_http_version 1.1;" in authorize
    assert 'proxy_set_header Connection "";' in authorize
    assert "include /etc/nginx/nginx-identity-strip.conf;" in authorize


def test_gateway_relays_all_bounded_platform_renewal_cookies() -> None:
    """The auth subrequest's numbered cookies must survive the backend boundary."""
    backend_auth = (REPO_ROOT / "frontend" / "nginx-backend-auth.conf").read_text(encoding="utf-8")

    for index in range(1, 5):
        assert (
            "auth_request_set $jarvis_auth_cookie_"
            f"{index} $upstream_http_x_jarvis_set_cookie_{index};"
        ) in backend_auth
        assert f"add_header Set-Cookie $jarvis_auth_cookie_{index} always;" in backend_auth
    assert "proxy_hide_header Set-Cookie;" in backend_auth
    assert 'proxy_set_header Cookie "";' in backend_auth


def test_restore_upload_ingress_exists_in_every_same_origin_mode():
    """Invariant: every same-origin ingress routes /restore-upload/ to the uploader.

    The in-browser off-host recovery upload PUTs to /restore-upload/<filename> on
    the same origin as the dashboard. Caddy fronts only the optional TLS profiles;
    localhost, LAN, and the Cloudflare tunnel all terminate at frontend/nginx.conf —
    without a location there the PUT falls into the SPA fallback and the advertised
    flow silently breaks. Dropping the route from either Caddyfile would break the
    TLS modes the same way.
    """
    nginx = (REPO_ROOT / "frontend" / "nginx.conf").read_text()
    block = _brace_block(nginx, r"location \^~ /restore-upload/")
    assert "proxy_pass http://restore-uploader:8090;" in block, (
        "frontend/nginx.conf must proxy /restore-upload/ to restore-uploader:8090"
    )
    # Multi-GB archive streams: nginx must not cap or buffer the body — the
    # uploader itself enforces UPLOAD_MAX_GB.
    assert "client_max_body_size 0;" in block
    assert "proxy_request_buffering off;" in block

    for name in ("Caddyfile", "Caddyfile.local"):
        text = (REPO_ROOT / "caddy" / name).read_text()
        handle = _brace_block(text, r"handle /restore-upload/\*")
        assert "reverse_proxy http://restore-uploader:8090" in handle, (
            f"caddy/{name} must reverse_proxy /restore-upload/* to restore-uploader:8090"
        )


def test_public_caddy_hsts_is_host_only_and_not_preloaded():
    """The default public policy must not make claims for sibling hostnames.

    ``includeSubDomains`` is safe only when every current and future subdomain
    supports HTTPS, while browser preload requires a separate deliberate
    submission. JARVIS owns neither commitment for an operator's domain.
    """
    text = (REPO_ROOT / "caddy" / "Caddyfile").read_text()
    assert 'header Strict-Transport-Security "max-age=31536000"' in text
    assert "includeSubDomains" not in text
    assert "preload" not in text.lower()
    for header in ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in text


# Every compose secret must have a declared provisioning path. "update" secrets
# are created by scripts/init-secrets.sh, which setup.sh and update.sh both run
# before any container is touched. "setup" secrets are created only at setup
# time by scripts/gen-langfuse-keys.sh: Langfuse headless init is write-once,
# so recreating that keypair during an update would 401 against an
# already-provisioned volume. A compose secret with no provisioning path is how
# the v1.1.3 upgrade failure shipped — declared for every service start,
# created by nothing on the update path.
SECRET_PROVISIONING = {
    "postgres_platform_runtime_password": "update",
    "postgres_research_runtime_password": "update",
    "postgres_learning_runtime_password": "update",
    "postgres_migrator_password": "update",
    "postgres_cluster_bootstrap_password": "update",
    "postgres_legacy_rollback_password": "update",
    "postgres_backup_reader_password": "update",
    "postgres_restore_operator_password": "update",
    "postgres_erasure_executor_password": "update",
    "litellm_runtime_password": "update",
    "litellm_migrator_password": "update",
    "postgres_legacy_source_password": "update",
    "jarvis_api_key": "update",
    "jarvis_setup_token": "update",
    "platform_identity_private_key": "update",
    "platform_identity_public_key": "update",
    "telegram_service_token": "update",
    "research_service_token": "update",
    "learning_service_token": "update",
    "jarvis_model_hmac_key": "update",
    "telegram_bot_token": "update",
    "qdrant_api_key": "update",
    "smtp_pass": "update",
    "litellm_master_key": "update",
    "litellm_salt_key": "update",
    "jarvis_config_key": "update",
    "langfuse_pg_password": "update",
    "langfuse_nextauth_secret": "update",
    "langfuse_salt": "update",
    "cloudflare_tunnel_token": "update",
    "backup_encrypt_key": "update",
    "langfuse_init_pk": "setup",
    "langfuse_init_sk": "setup",
}


def _secret_sources(service: dict[str, Any]) -> set[str]:
    return {
        entry if isinstance(entry, str) else entry["source"] for entry in service.get("secrets", [])
    }


def test_vector_is_socketless_and_cannot_write_product_logs(compose) -> None:
    """Optional aggregate logging must not read Docker or call Research."""
    vector = compose["services"]["vector"]
    rendered = json.dumps(vector)
    assert "/var/run/docker.sock" not in rendered
    assert "infra_ingest_key" not in rendered
    assert "paper_ingestion" not in vector.get("depends_on", {})
    assert "vector_data" not in compose.get("volumes", {})
    vector_config = (REPO_ROOT / "infra/vector.toml").read_text(encoding="utf-8")
    assert "docker_logs" not in vector_config
    assert "/infra-events" not in vector_config
    assert "$1" not in vector_config


def test_observability_make_target_does_not_enable_telegram() -> None:
    """Optional telemetry excludes Telegram and refreshes gateway routing."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    target = re.search(r"(?ms)^observability-up:.*?(?=^\S|\Z)", makefile)

    assert target is not None
    assert "telegram_bot" not in target.group()
    assert "vector platform_api paper_ingestion learning_engine dashboard" in target.group()
    assert "restart dashboard" in target.group()


def test_database_credentials_are_runtime_scoped_and_migrations_gate_startup(compose):
    """Application services receive one database password after migrations complete."""
    expected = {
        "platform_api": "postgres_platform_runtime_password",
        "paper_ingestion": "postgres_research_runtime_password",
        "learning_engine": "postgres_learning_runtime_password",
        "litellm": "litellm_runtime_password",
    }
    forbidden = {
        "postgres_migrator_password",
        "postgres_cluster_bootstrap_password",
        "postgres_legacy_rollback_password",
        "postgres_backup_reader_password",
        "postgres_restore_operator_password",
        "postgres_erasure_executor_password",
        "litellm_migrator_password",
    }
    for service_name, password_secret in expected.items():
        service = compose["services"][service_name]
        secrets = _secret_sources(service)
        assert password_secret in secrets
        assert not secrets & forbidden
        dependency = (
            "litellm-migrator" if service_name == "litellm" else "migration-authority-finalize"
        )
        assert service["depends_on"][dependency]["condition"] == "service_completed_successfully"
        if service_name != "litellm":
            assert "./db:/app/db:ro" in service["volumes"]

    assert _secret_sources(compose["services"]["telegram_bot"]).isdisjoint(set(expected.values()))
    assert (
        compose["services"]["litellm"]["environment"]["POSTGRES_USER"] == "jarvis_litellm_runtime"
    )
    assert compose["services"]["jarvis-migrator"]["volumes"] == ["./db:/app/db:ro"]
    assert _secret_sources(compose["services"]["cluster-bootstrap"]) == {
        "postgres_platform_runtime_password",
        "postgres_research_runtime_password",
        "postgres_learning_runtime_password",
        "postgres_migrator_password",
        "postgres_cluster_bootstrap_password",
        "postgres_legacy_rollback_password",
        "postgres_backup_reader_password",
        "postgres_restore_operator_password",
        "postgres_erasure_executor_password",
        "litellm_runtime_password",
        "litellm_migrator_password",
        "postgres_legacy_source_password",
    }
    finalizer = compose["services"]["migration-authority-finalize"]
    assert finalizer["command"] == ["finalize"]
    assert _secret_sources(finalizer) == {"postgres_cluster_bootstrap_password"}
    assert finalizer["depends_on"]["jarvis-migrator"]["condition"] == (
        "service_completed_successfully"
    )
    executor = compose["services"]["erasure-executor"]
    assert executor["restart"] == "unless-stopped"
    assert executor["command"] == ["python", "-m", "platform_api.erasure_executor"]
    assert _secret_sources(executor) == {"postgres_erasure_executor_password"}
    assert executor["environment"] == {
        "POSTGRES_USER": "jarvis_erasure_executor",
        "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_erasure_executor_password",
    }


def test_backup_and_restore_split_credential_lifetimes(compose):
    """Scheduled backup lacks restore authority; the restore job is one-shot only."""
    backup = compose["services"]["postgres-backup"]
    assert _secret_sources(backup) == {
        "postgres_backup_reader_password",
        "backup_encrypt_key",
        "qdrant_api_key",
    }
    assert backup["environment"]["PGUSER"] == "jarvis_backup_reader"
    assert backup["environment"]["POSTGRES_PASSWORD_FILE"].endswith(
        "postgres_backup_reader_password"
    )
    assert "postgres_restore_operator_password" not in str(backup)
    assert "/usr/local/bin/restore.sh" not in "\n".join(backup["volumes"])
    assert "/host-secrets" not in "\n".join(backup["volumes"])
    assert "./secrets:/secrets:ro" not in backup["volumes"]
    assert "${JARVIS_STATE_DIR:-./secrets}:/backup-state:rw" not in backup["volumes"]
    assert backup["environment"]["SECRETS_DIR"] == "/data-keys"
    assert backup["environment"]["HOST_SECRETS_DIR"] == "/backup-state"

    restore = compose["services"]["postgres-restore"]
    assert restore["profiles"] == ["restore"]
    assert restore["restart"] == "no"
    assert _secret_sources(restore) == {
        "postgres_restore_operator_password",
        "backup_encrypt_key",
        "qdrant_api_key",
    }
    assert restore["environment"]["PGUSER"] == "jarvis_restore_operator"
    assert restore["environment"]["POSTGRES_PASSWORD_FILE"].endswith(
        "postgres_restore_operator_password"
    )
    assert "/usr/local/bin/restore.sh" in "\n".join(restore["volumes"])
    assert "/host-secrets" in "\n".join(restore["volumes"])
    assert restore["entrypoint"] == ["/usr/local/bin/restore.sh"]
    assert restore["command"] == ["--run-request"]
    bootstrap_mounts = set(compose["services"]["cluster-bootstrap"]["volumes"])
    assert "./db/restore-authority.sql:/app/db/restore-authority.sql:ro" in bootstrap_mounts


def test_cluster_bootstrap_scopes_legacy_conversion_authority() -> None:
    """Floor 113 may bridge owners only until migration 0114 revokes the bridge."""
    source = (REPO_ROOT / "scripts" / "postgres-role-bootstrap.sh").read_text(encoding="utf-8")
    owners = "jarvis_platform_owner jarvis_research_owner jarvis_learning_owner jarvis_ops_owner"
    assert f'owner_roles="{owners}"' in source
    assert "GRANT ${owner_role} TO jarvis_migrator WITH ADMIN OPTION, INHERIT FALSE" in source
    assert "GRANT ${owner_role} TO jarvis_legacy_rollback WITH INHERIT FALSE" in source
    assert (
        "GRANT jarvis_legacy_rollback TO jarvis_migrator WITH ADMIN OPTION, INHERIT FALSE" in source
    )
    assert "REVOKE ${owner_role} FROM jarvis_legacy_rollback" in source
    assert "REVOKE ${owner_role} FROM jarvis_migrator" in source
    assert "CREATE ROLE %s LOGIN SUPERUSER NOINHERIT" in source
    assert "prepare|finalize|restore-prepare|restore-finalize" in source
    assert 'if [ "$mode" = "restore-prepare" ]' in source
    assert 'if [ "$mode" = "restore-finalize" ]' in source
    assert 'authority_file="/app/db/restore-authority.sql"' in source
    assert "ALTER ROLE jarvis NOLOGIN" in source
    assert "public.schema_migrations TO jarvis_migrator" in source
    assert "GRANT CREATE ON DATABASE ${database} TO ${owner_role}" in source
    assert "REVOKE CREATE ON DATABASE ${database} FROM ${owner_role}" in source
    assert "postgres_legacy_source_password" in source
    assert "transfer_owned_objects litellm jarvis_cluster_bootstrap" in source
    assert "REASSIGN OWNED BY jarvis_cluster_bootstrap" not in source
    assert 'if [ "$mode" = "finalize" ]' in source
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE ${database} FROM PUBLIC" in source
    assert "ALTER ROLE jarvis_restore_operator WITH CREATEDB INHERIT" in source
    assert "GRANT pg_signal_backend TO jarvis_restore_operator" in source
    assert "assert_recovery_roles" in source
    assert source.count("assert_recovery_roles") == 4
    assert "backup or restore role authority is invalid" in source


PROVISIONING_SCRIPTS = {
    "update": "scripts/init-secrets.sh",
    "setup": "scripts/gen-langfuse-keys.sh",
}


def test_every_compose_secret_has_a_declared_provisioning_path(compose):
    declared = compose.get("secrets") or {}

    unclassified = sorted(set(declared) - set(SECRET_PROVISIONING))
    assert not unclassified, (
        f"compose secrets without a provisioning path: {unclassified} — add each to "
        "scripts/init-secrets.sh and mark it 'update' in SECRET_PROVISIONING, or mark "
        "it 'setup' there with a rationale if it must only be created at setup time"
    )
    stale = sorted(set(SECRET_PROVISIONING) - set(declared))
    assert not stale, f"SECRET_PROVISIONING lists secrets compose no longer declares: {stale}"

    for name, definition in declared.items():
        expected_file = (
            "./secrets/postgres_password.txt"
            if name == "postgres_legacy_source_password"
            else f"./secrets/{name}.txt"
        )
        assert definition.get("file") == expected_file, (
            f"{name}: compose secret file must be ./secrets/{name}.txt"
        )

    scripts = {}
    for mode, path in PROVISIONING_SCRIPTS.items():
        lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
        scripts[mode] = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    for name, mode in SECRET_PROVISIONING.items():
        provisioned_filename = (
            "postgres_password.txt" if name == "postgres_legacy_source_password" else f"{name}.txt"
        )
        assert provisioned_filename in scripts[mode], (
            f"{name}: declared '{mode}' but {PROVISIONING_SCRIPTS[mode]} never touches "
            f"{name}.txt outside comments"
        )

    for runner in ("update.sh", "setup.sh"):
        assert "init-secrets.sh" in (REPO_ROOT / runner).read_text(encoding="utf-8"), (
            f"{runner} no longer runs scripts/init-secrets.sh, so 'update' secrets "
            "would not be provisioned before containers are touched"
        )


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())


def _built_images(job: dict[str, Any]) -> set[tuple[str, str]]:
    """Return the normalized (context, dockerfile) pairs a build matrix covers."""
    return {
        (posixpath.normpath(entry["context"]), posixpath.normpath(entry["file"]))
        for entry in job["strategy"]["matrix"]["include"]
    }


def test_build_smoke_gates_every_published_image() -> None:
    """A published image must be built by the merge gate before it can be pushed.

    ghcr-publish.yml is the only thing that pushes to the registry, so its own
    matrix is the definition of "published". The build smoke test enumerated its
    images by hand and drifted, which let platform_api and restore_uploader be
    published without ever building on a pull request.
    """
    published = _built_images(_workflow("ghcr-publish.yml")["jobs"]["build"])
    gated = _built_images(_workflow("security.yml")["jobs"]["docker-build-smoke"])

    assert published, "ghcr-publish.yml declares no images to publish"
    ungated = sorted(published - gated)
    assert not ungated, (
        f"published but never built by docker-build-smoke: {ungated} — add each "
        "(context, file) pair to the matrix in .github/workflows/security.yml"
    )


SECRET_INVENTORY_HEADING = "#### Secret inventory"
SECRET_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|")


def _documented_secrets() -> set[str]:
    """Return the secret names listed in the deployment guide's inventory table."""
    lines = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(SECRET_INVENTORY_HEADING)
    documented: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        match = SECRET_ROW.match(line)
        if match:
            documented.add(match.group(1))
    return documented


def test_documented_secret_inventory_matches_compose(compose) -> None:
    """Every credential an operator must hold is documented, and nothing else is.

    The guide described a single database password long after the release split
    it into eleven per-role logins, so an operator reading it could not tell
    which files a deployment actually needs.
    """
    declared = set(compose.get("secrets") or {})
    documented = _documented_secrets()

    assert documented, (
        f"no secret rows found under '{SECRET_INVENTORY_HEADING}' in docs/DEPLOYMENT.md"
    )
    undocumented = sorted(declared - documented)
    assert not undocumented, (
        f"compose secrets missing from the deployment guide: {undocumented} — add a "
        f"row for each under '{SECRET_INVENTORY_HEADING}' in docs/DEPLOYMENT.md"
    )
    retired = sorted(documented - declared)
    assert not retired, (
        f"the deployment guide documents secrets compose no longer declares: {retired}"
    )
