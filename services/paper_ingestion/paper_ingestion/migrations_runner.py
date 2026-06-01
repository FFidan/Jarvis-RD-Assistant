"""Deprecated re-export shim — use ``jarvis_common.migrations`` directly.

The migration runner moved to ``jarvis_common.migrations`` so non-paper-ingestion
services (learning_engine, future workers) can share it.
This shim is preserved so the 9+ paper_ingestion test files importing
``from paper_ingestion.migrations_runner import run_migrations`` keep working.
Eventually remove once those tests migrate to the new import path.
"""

from jarvis_common import migrations as _migrations

_MIGRATION_SCHEMA_PROBES = _migrations._MIGRATION_SCHEMA_PROBES
_TXN_LINE_RE = _migrations._TXN_LINE_RE
_repair_false_applied_migrations = _migrations._repair_false_applied_migrations
_strip_outer_transaction_control = _migrations._strip_outer_transaction_control
run_migrations = _migrations.run_migrations

__all__ = [
    "_MIGRATION_SCHEMA_PROBES",
    "_TXN_LINE_RE",
    "_repair_false_applied_migrations",
    "_strip_outer_transaction_control",
    "run_migrations",
]
