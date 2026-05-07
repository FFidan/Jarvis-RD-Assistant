"""Deprecated re-export shim — use ``jarvis_common.migrations`` directly.

The migration runner moved to ``jarvis_common.migrations`` as part of W3-DRY-6
so non-paper-ingestion services (learning_engine, future workers) can share it.
This shim is preserved so the 9+ paper_ingestion test files importing
``from paper_ingestion.migrations_runner import run_migrations`` keep working.
Eventually remove once those tests migrate to the new import path.
"""

from jarvis_common.migrations import (
    _MIGRATION_SCHEMA_PROBES as _MIGRATION_SCHEMA_PROBES,
)
from jarvis_common.migrations import (
    _TXN_LINE_RE as _TXN_LINE_RE,
)
from jarvis_common.migrations import (
    _repair_false_applied_migrations as _repair_false_applied_migrations,
)
from jarvis_common.migrations import (
    _strip_outer_transaction_control as _strip_outer_transaction_control,
)
from jarvis_common.migrations import (
    run_migrations as run_migrations,
)

__all__ = [
    "_MIGRATION_SCHEMA_PROBES",
    "_TXN_LINE_RE",
    "_repair_false_applied_migrations",
    "_strip_outer_transaction_control",
    "run_migrations",
]
