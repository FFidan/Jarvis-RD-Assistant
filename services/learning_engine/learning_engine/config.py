"""Typed pydantic-settings configuration for the Learning Engine service.

Bucket H — Wave 4: migrates learning-engine-specific ``os.getenv`` call sites
to a typed ``LearningEngineSettings`` class. Inherits shared infra keys from
``JarvisCommonSettings``.

1:1 env-var table (learning-engine layer)
------------------------------------------
Env var                   Field                     Call sites
---                       ---                       ---
SNAPSHOT_STORAGE_PATH     snapshot_storage_path     card_generator.py
MULTITENANT_ENABLED       multitenant_enabled       main.py
"""

from __future__ import annotations

from jarvis_common.config import JarvisCommonSettings
from pydantic import Field

__all__ = [
    "LearningEngineSettings",
    "get_learning_engine_settings",
]


class LearningEngineSettings(JarvisCommonSettings):
    """Typed settings for the Learning Engine service env vars.

    Extends ``JarvisCommonSettings`` with LE-specific keys.  All fields map
    1:1 to the existing env vars — no drops, no renames.
    """

    # --- Storage paths --------------------------------------------------
    snapshot_storage_path: str = Field(
        default="/data/snapshots",
        description=(
            "Directory for paper analysis snapshot files (SNAPSHOT_STORAGE_PATH). "
            "Learning Engine reads snapshots written by Paper Ingestion's "
            "summarization pipeline when generating cards."
        ),
    )

    # --- Multi-tenancy --------------------------------------------------
    multitenant_enabled: bool = Field(
        default=False,
        description=(
            "Enable multi-tenant mode (MULTITENANT_ENABLED).  When true, "
            "auth resolver enforces ownership checks.  Currently a stub — "
            "enabling logs CRITICAL to warn operators."
        ),
    )


def get_learning_engine_settings() -> LearningEngineSettings:
    """Return a fresh ``LearningEngineSettings`` snapshot.

    Intentionally uncached so that ``monkeypatch.setenv`` works in tests.
    """
    return LearningEngineSettings()
