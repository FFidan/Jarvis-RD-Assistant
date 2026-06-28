"""Shared setup for the live-PG contract suite."""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _contract_model_signing_key() -> None:
    """Inviting or restoring a user crosses a deployment into multi-user, which
    now requires a configured model-signing key. The contract suite models a
    configured deployment, so provide one for the whole session; a key already
    present in the environment still wins."""
    os.environ.setdefault("JARVIS_MODEL_HMAC_KEY", "x" * 32)
