"""Root pytest conftest — applies to all test directories.

Freezes the Research and Learning identity-middleware decision (see below) and
exports a single autouse fixture that clears the
``jarvis_common.settings.get_secrets_settings`` lru_cache before AND after
every test. Tests that monkeypatch secret env vars (``LITELLM_MASTER_KEY``,
``JARVIS_API_KEY``, …) need a fresh ``SecretsSettings`` snapshot rather than
the cached one that was built when an earlier test ran without those vars
set. The cached factory is the right default for production (cheap startup
amortisation); this fixture neutralises it under pytest.
"""

from __future__ import annotations

import os

import pytest

# Production requires a Platform-signed assertion on every protected Research
# and Learning route, and both applications decide while being imported whether
# to install the middleware that enforces it. Most unit and contract tests drive
# those applications directly, without the gateway that mints assertions, so the
# decision is taken once here with the requirement disabled. The variable is then
# removed: past that point it is no longer a middleware switch but an ordinary
# configuration value, and production validation must read it at its deployed
# default rather than at a test opt-out.
os.environ["JARVIS_IDENTITY_ASSERTIONS_REQUIRED"] = "false"

import learning_engine.main  # noqa: E402, F401
import paper_ingestion.main  # noqa: E402, F401

del os.environ["JARVIS_IDENTITY_ASSERTIONS_REQUIRED"]


@pytest.fixture(autouse=True)
def _clear_secrets_cache():
    from jarvis_common.settings import get_secrets_settings
    from platform_api.config import get_platform_settings

    get_secrets_settings.cache_clear()
    get_platform_settings.cache_clear()
    yield
    get_secrets_settings.cache_clear()
    get_platform_settings.cache_clear()
