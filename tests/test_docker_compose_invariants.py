"""
Test docker-compose.yml invariants.

Ensures critical services have proper secret mounts (e.g., langfuse tracing keys)
and that locally-built images carry explicit pull semantics.
"""

from pathlib import Path

import pytest
import yaml

BUILT_SERVICES = {"paper_ingestion", "learning_engine", "telegram_bot", "dashboard", "langfuse"}


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
    must declare an explicit `pull_policy` — the jarvis/* tags are unpublished,
    so an implicit registry pull can only fail ("pull access denied").
    """
    built = {
        name: svc for name, svc in compose["services"].items() if "image" in svc and "build" in svc
    }
    assert set(built) == BUILT_SERVICES, (
        f"image+build service set changed: {set(built) ^ BUILT_SERVICES} — "
        "update BUILT_SERVICES and give any new service an explicit pull_policy"
    )
    for name, svc in built.items():
        assert svc.get("pull_policy") == "build", (
            f"{name} must declare pull_policy: build; got {svc.get('pull_policy')!r}"
        )
