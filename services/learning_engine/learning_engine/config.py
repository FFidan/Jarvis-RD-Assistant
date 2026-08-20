"""Typed pydantic-settings configuration for the Learning Engine service.

Bucket H — migrates learning-engine-specific ``os.getenv`` call sites to a
typed ``LearningEngineSettings`` class. Inherits shared infra keys from
``JarvisCommonSettings``.

1:1 env-var table (learning-engine layer)
------------------------------------------
Env var                   Field                     Call sites
---                       ---                       ---
SNAPSHOT_STORAGE_PATH     snapshot_storage_path     card_generator.py
"""

from __future__ import annotations

from pathlib import Path

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
    platform_api_url: str = Field(
        default="http://platform_api:8003",
        description="Platform signer origin for owner-command authorization (PLATFORM_API_URL).",
    )
    paper_ingestion_url: str = Field(
        default="http://paper_ingestion:8000",
        description="Research owner API origin for domain commands (PAPER_INGESTION_URL).",
    )
    learning_service_token_file: Path = Field(
        default=Path("/run/secrets/learning_service_token"),
        description="Learning-only service credential used for owner commands.",
    )


def get_learning_engine_settings() -> LearningEngineSettings:
    """Return a fresh ``LearningEngineSettings`` snapshot.

    Intentionally uncached so that ``monkeypatch.setenv`` works in tests.
    """
    return LearningEngineSettings()
