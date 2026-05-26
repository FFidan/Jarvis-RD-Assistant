"""
Test docker-compose.yml invariants.

Ensures critical services have proper secret mounts (e.g., langfuse tracing keys).
"""

from pathlib import Path

import yaml


def test_telegram_bot_has_langfuse_init_secrets():
    """
    Verify that telegram_bot service has langfuse_init_pk and langfuse_init_sk
    mounted as Docker secrets.

    Without these, the langfuse client silently fails to initialize tracing
    (LANGFUSE_PUBLIC_KEY_FILE and LANGFUSE_SECRET_KEY_FILE point to missing files).
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())

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


def test_langfuse_service_secrets_mounted_and_not_in_env():
    """SEC-HIGH-02 invariant: langfuse service must mount the 3 secrets at
    /run/secrets/ AND must NOT carry DATABASE_URL/NEXTAUTH_SECRET/SALT as
    plaintext env vars (which would be visible via `docker inspect`).
    """
    compose_path = Path(__file__).parent.parent / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    langfuse = compose["services"]["langfuse"]

    mounted = {s if isinstance(s, str) else s.get("source") for s in langfuse.get("secrets", [])}
    required = {"langfuse_pg_password", "langfuse_nextauth_secret", "langfuse_salt"}
    assert required.issubset(mounted), f"langfuse service must mount {required}; got {mounted}"

    env = langfuse.get("environment", {}) or {}
    forbidden = {"DATABASE_URL", "NEXTAUTH_SECRET", "SALT"}
    leaked = forbidden & set(env.keys())
    assert not leaked, (
        f"SEC-HIGH-02 regression: {leaked} re-introduced as plaintext env vars "
        f"on langfuse service — `docker inspect` would expose them."
    )
