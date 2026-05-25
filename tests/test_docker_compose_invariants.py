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
