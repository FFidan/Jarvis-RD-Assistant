"""Root pytest conftest — applies to all test directories.

Currently exports a single autouse fixture that clears the
``jarvis_common.settings.get_secrets_settings`` lru_cache before AND after
every test. Tests that monkeypatch secret env vars (``LITELLM_MASTER_KEY``,
``JARVIS_API_KEY``, …) need a fresh ``SecretsSettings`` snapshot rather than
the cached one that was built when an earlier test ran without those vars
set. The cached factory is the right default for production (cheap startup
amortisation); this fixture neutralises it under pytest.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_secrets_cache():
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()
    yield
    get_secrets_settings.cache_clear()
