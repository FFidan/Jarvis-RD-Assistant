"""
Test docker-compose.yml invariants.

Ensures critical services have proper secret mounts (e.g., langfuse tracing keys)
and that locally-built images carry explicit pull semantics.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent

# Every service that pairs an `image:` tag with a `build:` context (drift guard).
BUILT_SERVICES = {
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


def test_telegram_bot_has_langfuse_init_secrets(compose):
    """
    Verify that telegram_bot service has langfuse_init_pk and langfuse_init_sk
    mounted as Docker secrets.

    Without these, the langfuse client silently fails to initialize tracing
    (LANGFUSE_PUBLIC_KEY_FILE and LANGFUSE_SECRET_KEY_FILE point to missing files).
    """
    secrets = compose["services"]["telegram_bot"]["secrets"]
    # Handle both string entries and dict entries (e.g., {source: ..., target: ...})
    secret_names = [s if isinstance(s, str) else s.get("source") for s in secrets]

    assert "langfuse_init_pk" in secret_names, (
        "telegram_bot service missing langfuse_init_pk secret mount; "
        "langfuse tracing will be silently disabled"
    )
    assert "langfuse_init_sk" in secret_names, (
        "telegram_bot service missing langfuse_init_sk secret mount; "
        "langfuse tracing will be silently disabled"
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
    for script in ("setup.sh", "update.sh", "scripts/jarvis-setup.sh"):
        text = (REPO_ROOT / script).read_text()
        assert "PUBLISHED_SERVICES_BASE" in text, (
            f"{script} starts the stack but does not pull the shared published set — "
            "any image it leaves missing would be silently BUILT by `up`"
        )


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
