"""Backwards-compatible re-export shim. All implementations live in submodules.

Submodules:
- config_metadata: regex constants, key classification
- config_validators: validators + _CONFIG_VALIDATORS registry
- config_db: row-level DB I/O + migrate_plaintext_secrets
- model_assignment: model/provider validation + Telegram nudge reload
- analytics_queries: analytics SQL helpers
- scheduler_effects: cron rescheduling with rollback
- provider_test: provider connectivity probe
- data_export: GDPR ZIP export
- config_write: top-level write_config orchestration
"""

# _log_event is re-exported here because routers.settings patches it via
# the settings_service namespace (legacy patch path).
from jarvis_common.event_log import log_event as _log_event  # noqa: F401

from paper_ingestion.services.analytics_queries import *  # noqa: F401, F403
from paper_ingestion.services.config_db import *  # noqa: F401, F403
from paper_ingestion.services.config_metadata import *  # noqa: F401, F403
from paper_ingestion.services.config_validators import *  # noqa: F401, F403
from paper_ingestion.services.config_write import *  # noqa: F401, F403
from paper_ingestion.services.data_export import *  # noqa: F401, F403
from paper_ingestion.services.model_assignment import *  # noqa: F401, F403
from paper_ingestion.services.provider_test import *  # noqa: F401, F403
from paper_ingestion.services.scheduler_effects import *  # noqa: F401, F403
