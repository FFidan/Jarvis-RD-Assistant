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

    setup_sh = (REPO_ROOT / "setup.sh").read_text()
    telegram = re.search(r"^\s*PUBLISHED_SERVICE_TELEGRAM=(\S+)", setup_sh, re.MULTILINE)
    assert telegram, "setup.sh must declare PUBLISHED_SERVICE_TELEGRAM"
    setup_named = _bash_array_items(setup_sh, "PUBLISHED_SERVICES_BASE") | {telegram.group(1)}
    assert setup_named == PUBLISHED_SERVICES, (
        "setup.sh does not pull exactly the published services; unpulled ones would be "
        f"silently BUILT. Difference: {setup_named ^ PUBLISHED_SERVICES}"
    )

    update_named = _bash_array_items((REPO_ROOT / "update.sh").read_text(), "APP_SERVICES")
    assert update_named == PUBLISHED_SERVICES, (
        "update.sh does not refresh exactly the published services; missing ones would be "
        f"silently BUILT. Difference: {update_named ^ PUBLISHED_SERVICES}"
    )
