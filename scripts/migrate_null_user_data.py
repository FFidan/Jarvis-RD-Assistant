#!/usr/bin/env python3
"""Resolve NULL-user rows in the WS-USER-DELETION cascade tables.

Migration 080 refuses to run while any of the 17 owned-data tables holds a
NULL ``user_id`` row (those rows would orphan under ON DELETE CASCADE). This
script resolves them according to ``JARVIS_NULL_USER_MIGRATION_TARGET``:

  * ``first_admin`` (default) — reassign every NULL-user row to the oldest
    admin user (the canonical-corpus owner fallback).
  * ``delete``                — delete the NULL-user rows outright.
  * ``fail``                  — report counts and exit non-zero without
                                touching any data (dry-run / CI gate).

Before any destructive op (delete, or reassignment) a per-table backup table
``null_user_backup_<table>_<ts>`` is created with the affected rows. All work
runs inside a single transaction: a failure rolls everything back.

``papers`` is intentionally excluded: a NULL ``papers.discovered_by`` is a
shared/system paper (canonical-corpus model), not an orphan.

Usage:
    JARVIS_NULL_USER_MIGRATION_TARGET=first_admin \
        python scripts/migrate_null_user_data.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

from scripts.audit_null_user_data import CASCADE_TABLES

_VALID_TARGETS = ("first_admin", "delete", "fail")


def resolve_target() -> str:
    target = os.environ.get("JARVIS_NULL_USER_MIGRATION_TARGET", "first_admin").strip()
    if target not in _VALID_TARGETS:
        raise SystemExit(
            f"Invalid JARVIS_NULL_USER_MIGRATION_TARGET={target!r}; "
            f"expected one of {_VALID_TARGETS}"
        )
    return target


def backup_table_name(table: str, ts: str) -> str:
    """Deterministic backup-table identifier (unit-testable, no DB)."""
    return f"null_user_backup_{table}_{ts}"


async def _run(target: str) -> int:
    import asyncpg  # noqa: PLC0415

    from scripts._db import get_dsn  # noqa: PLC0415

    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    conn = await asyncpg.connect(get_dsn())
    try:
        summary: dict[str, int] = {}
        async with conn.transaction():
            first_admin_id: int | None = None
            if target == "first_admin":
                first_admin_id = await conn.fetchval(
                    "SELECT id FROM users WHERE role = 'admin' AND deleted_at IS NULL "
                    "ORDER BY created_at ASC, id ASC LIMIT 1"
                )
                if first_admin_id is None:
                    raise SystemExit("first_admin target requested but no active admin user exists")

            for table in CASCADE_TABLES:
                n = int(await conn.fetchval(f"SELECT count(*) FROM {table} WHERE user_id IS NULL"))
                if n == 0:
                    continue
                summary[table] = n

                if target == "fail":
                    continue

                # Backup the affected rows before any destructive change.
                backup = backup_table_name(table, ts)
                await conn.execute(
                    f'CREATE TABLE "{backup}" AS SELECT * FROM {table} WHERE user_id IS NULL'
                )

                if target == "first_admin":
                    await conn.execute(
                        f"UPDATE {table} SET user_id = $1 WHERE user_id IS NULL",
                        first_admin_id,
                    )
                else:  # delete
                    await conn.execute(f"DELETE FROM {table} WHERE user_id IS NULL")

            if target == "fail" and summary:
                # Surface counts then abort the (read-only) transaction.
                print(f"NULL-user rows found (target=fail, no changes): {summary}")
                raise SystemExit(2)
    finally:
        await conn.close()

    if not summary:
        print("No NULL-user rows found; nothing to migrate.")
    else:
        verb = "reassigned to first admin" if target == "first_admin" else "deleted"
        print(f"NULL-user rows {verb} (backups: null_user_backup_<table>_{ts}): {summary}")
    return 0


def main() -> int:
    target = resolve_target()
    return asyncio.run(_run(target))


if __name__ == "__main__":
    sys.exit(main())
