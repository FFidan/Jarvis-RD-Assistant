"""Tests that ci-smoke.sh provisions all required secrets (BUG-CISMOKE-1).

# Verified: scripts/ci-smoke.sh:24-42 (printf provisioning block)
# Verified: docker-compose.yml:768-796 (declared secrets that smoke must satisfy)
"""

from __future__ import annotations

from pathlib import Path

REQUIRED_SECRETS = (
    "jarvis_model_hmac_key",
    "langfuse_init_pk",
    "langfuse_init_sk",
)


def test_ci_smoke_provisions_all_required_secrets() -> None:
    """ci-smoke.sh must provision every secret declared in docker-compose.yml."""
    script = Path("scripts/ci-smoke.sh").read_text()
    missing = [name for name in REQUIRED_SECRETS if name not in script]
    assert not missing, f"ci-smoke.sh missing secret provisioning: {missing}"
