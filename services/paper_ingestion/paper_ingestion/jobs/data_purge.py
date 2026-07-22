"""Daily hard-delete of soft-deleted users past the 30-day grace.

User deletion D2: ``DELETE /api/admin/users/{id}`` only sets
``users.deleted_at``. This job hard-deletes any user whose grace window has
elapsed; migration 080's ON DELETE CASCADE FKs then collapse every owned row
(papers.discovered_by stays ON DELETE SET NULL because it is audit metadata,
not an authorization field).

GDPR compliance: Qdrant points may carry the ingesting ``user_id`` as audit
metadata. Before SQL deletion, unreferenced points are removed and points that
remain visible to surviving users have that identifier redacted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from jarvis_common.audit import log_audit
from jarvis_common.maintenance import skip_for_maintenance

logger = logging.getLogger(__name__)

# Daily at 03:10 — before the 04:00 Pulse deck, after classifier training.
_DATA_PURGE_CRON = "10 3 * * *"

_SELECT_EXPIRED_USERS = (
    "SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'"
)
# Parameterized DELETE: $1 is a list[int] of uids whose Qdrant purge FAILED.
# Those uids are excluded so their rows survive this run and are retried next
# night once Qdrant is healthy again.  When $1 is an empty array every expired
# row is deleted (``id <> ALL('{}'::int[])`` is always true), which is
# regression-equivalent to the old unconditional delete.
_DELETE_EXPIRED_USERS_EXCLUDING = (
    "DELETE FROM users"
    " WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'"
    " AND id <> ALL($1::int[])"
)

# audit_log.user_id is a free-text column (NOT an FK), so DELETE FROM users does
# not cascade to it — a purged user's audit rows, and any PII in their metadata,
# survive the hard-delete unless explicitly anonymized. The table is protected
# by the no_update_audit_log RULE (db/init.sql); RULEs are query-rewrite and
# role-independent, so SECURITY DEFINER / triggers do NOT bypass them. The only
# way to erase is DISABLE RULE -> UPDATE -> ENABLE RULE in one transaction.
_AUDIT_PII_METADATA_KEYS = [
    "ip",
    "client_ip",
    "raw_client_ip",
    "ua_prefix",
    "email",
    "new_email_hash",
    "user_agent",
    "username",
    "telegram_username",
    "name",
]
_ANONYMIZE_AUDIT_LOG = (
    "UPDATE audit_log SET user_id = NULL, metadata = metadata - $2::text[]"
    " WHERE user_id = ANY($1::text[])"
)


@dataclass(frozen=True, slots=True)
class QdrantPurgeCounts:
    """Counts of Qdrant points deleted or retained with redacted audit data."""

    deleted: int
    redacted: int


async def _anonymize_audit_log_for_users(conn: Any, uids: list[int]) -> int:
    """Null the user_id and strip PII metadata from purged users' audit rows.

    The ALTER TABLE DISABLE/ENABLE RULE bracket is transactional and takes an
    AccessExclusiveLock, so the window is race-free; if the UPDATE fails the
    transaction rolls back and the rule is left enabled (no finally needed).
    The operational record (action, resource, timestamp) is retained.
    """
    if not uids:
        return 0
    uid_strs = [str(u) for u in uids]
    async with conn.transaction():
        await conn.execute("ALTER TABLE audit_log DISABLE RULE no_update_audit_log")
        result = await conn.execute(_ANONYMIZE_AUDIT_LOG, uid_strs, _AUDIT_PII_METADATA_KEYS)
        await conn.execute("ALTER TABLE audit_log ENABLE RULE no_update_audit_log")
    try:
        return int(result.split()[-1])
    except Exception:
        return 0


async def _purge_qdrant_for_user(
    qdrant: Any,
    uid: int,
    protected_paper_ids: list[int],
) -> QdrantPurgeCounts:
    """Erase a user's audit identifier from Qdrant before hard deletion.

    Parameters
    ----------
    qdrant : Any
        Async Qdrant client.
    uid : int
        User identifier being erased.
    protected_paper_ids : list[int]
        Papers that must retain their vectors because they are persisted public
        or belong to a surviving user's library.

    Returns
    -------
    QdrantPurgeCounts
        Exact pre-operation counts for deleted and redacted points.

    Notes
    -----
    Protected points are redacted before unprotected points are deleted. If
    either write fails, the exception propagates and the caller defers the SQL
    hard delete. ``set_payload`` changes only payload fields and preserves each
    point's vector.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchAny,
        MatchValue,
    )

    from paper_ingestion.ingestion.embedder import COLLECTION_NAME  # noqa: PLC0415

    uid_condition = FieldCondition(key="user_id", match=MatchValue(value=uid))
    protected_condition = (
        FieldCondition(key="paper_id", match=MatchAny(any=protected_paper_ids))
        if protected_paper_ids
        else None
    )
    delete_filter = Filter(
        must=[uid_condition],
        must_not=[protected_condition] if protected_condition is not None else None,
    )
    delete_count_result = await qdrant.count(
        collection_name=COLLECTION_NAME,
        count_filter=delete_filter,
        exact=True,
    )
    deleted = int(delete_count_result.count)

    redacted = 0
    if protected_condition is not None:
        redact_filter = Filter(must=[uid_condition, protected_condition])
        redact_count_result = await qdrant.count(
            collection_name=COLLECTION_NAME,
            count_filter=redact_filter,
            exact=True,
        )
        redacted = int(redact_count_result.count)
        await qdrant.set_payload(
            collection_name=COLLECTION_NAME,
            payload={"user_id": None},
            points=redact_filter,
            wait=True,
        )

    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=delete_filter,
        wait=True,
    )
    return QdrantPurgeCounts(deleted=deleted, redacted=redacted)


async def data_purge_task(app: Any) -> None:
    """Hard-delete users whose 30-day soft-delete grace has elapsed.

    Order of operations:
    1. Identify expired user ids (SELECT before DELETE so we know who to purge).
    2. For each uid, delete unreferenced Qdrant points and redact the audit
       identifier from retained points. A failed Qdrant operation defers that
       user's SQL hard delete without blocking other users.
    3. SQL DELETE FROM users (ON DELETE CASCADE collapses owned rows).
    4. Audit-log the destructive event via log_audit (best-effort, never raises).
    """
    if skip_for_maintenance("data purge"):
        return
    pool = app.state.db_pool
    qdrant = getattr(app.state, "qdrant_client", None)

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_EXPIRED_USERS)
            expired_ids: list[int] = [r["id"] for r in rows]

            if not expired_ids:
                logger.info("data_purge: no expired users to purge")
                return

            qdrant_vectors_deleted: dict[int, int] = {}
            qdrant_vectors_redacted: dict[int, int] = {}
            qdrant_errors: list[str] = []
            # Uids whose Qdrant purge failed — excluded from the hard DELETE so
            # their vectors are not orphaned.  They remain deleted_at-marked and
            # are naturally retried on the next nightly run.
            failed_uids: set[int] = set()
            # Public papers and papers held by a surviving user's library keep
            # their vectors even when the audit-attributed user is purged.
            protected_rows = await conn.fetch(
                """SELECT p.id AS paper_id
                   FROM papers p
                   WHERE p.visibility_scope = 'public'
                      OR EXISTS (
                          SELECT 1 FROM user_library ul
                          WHERE ul.paper_id = p.id
                            AND ul.user_id <> ALL($1::int[])
                      )""",
                expired_ids,
            )
            protected_paper_ids = [r["paper_id"] for r in protected_rows]
            if qdrant is not None:
                for uid in expired_ids:
                    try:
                        purge_counts = await _purge_qdrant_for_user(
                            qdrant, uid, protected_paper_ids
                        )
                        qdrant_vectors_deleted[uid] = purge_counts.deleted
                        qdrant_vectors_redacted[uid] = purge_counts.redacted
                    except Exception as exc:
                        logger.warning("data_purge: Qdrant purge failed for user %d: %r", uid, exc)
                        qdrant_errors.append(f"uid={uid}: {exc!r}")
                        failed_uids.add(uid)
            else:
                logger.warning(
                    "data_purge: qdrant_client not on app.state"
                    " — vectors NOT purged for %d user(s); deferring hard-delete",
                    len(expired_ids),
                )
                qdrant_errors.append("qdrant_client missing from app.state")
                # All uids are "failed" — none should be hard-deleted until
                # Qdrant is available and vectors can be removed first.
                failed_uids = set(expired_ids)

            # The hard-delete and the audit-log anonymization must be one atomic
            # unit: audit_log has no FK to users, so if the process crashes (or a
            # lock times out) between the autocommit DELETE and the anonymize, the
            # purged users' audit-log PII is permanently retained (GDPR breach).
            # Wrapping both in a single transaction makes them commit or roll back
            # together. The inner conn.transaction() in _anonymize_audit_log_for_users
            # auto-demotes to a SAVEPOINT here, and the ALTER TABLE DISABLE/ENABLE
            # RULE DDL participates in it, so a failure rolls the DISABLE back too.
            async with conn.transaction():
                # Only delete users whose Qdrant vectors were successfully purged.
                # Passing an empty list deletes all expired rows (regression-equivalent).
                result = await conn.execute(_DELETE_EXPIRED_USERS_EXCLUDING, list(failed_uids))

                # GDPR erasure: anonymize the hard-deleted users' audit_log rows
                # (audit_log has no FK to users, so the DELETE above does not reach it).
                purged_uids = [uid for uid in expired_ids if uid not in failed_uids]
                audit_rows_anonymized = await _anonymize_audit_log_for_users(conn, purged_uids)

        try:
            deleted: int | None = int(result.split()[-1])
        except Exception:
            # Parse failed — report unknown rather than an assumed count.
            deleted = None

        metadata: dict[str, Any] = {
            "user_ids": expired_ids,
            "users_deleted": deleted,
            "qdrant_vectors_deleted": qdrant_vectors_deleted,
            "qdrant_vectors_redacted": qdrant_vectors_redacted,
            "audit_rows_anonymized": audit_rows_anonymized,
        }
        if qdrant_errors:
            metadata["qdrant_errors"] = qdrant_errors

        await log_audit(
            pool=pool,
            action="user.hard_delete.purged",
            resource="users",
            metadata=metadata,
        )
        logger.info(
            "data_purge: hard-deleted %s expired user(s), qdrant_errors=%d",
            deleted,
            len(qdrant_errors),
        )
    except Exception:
        logger.exception("data_purge: failed to purge expired soft-deleted users")


def register_data_purge(scheduler: Any, app: Any) -> None:
    """Register :func:`data_purge_task` on *scheduler* (daily cron)."""
    try:
        scheduler.add_job(
            data_purge_task,
            trigger=CronTrigger.from_crontab(_DATA_PURGE_CRON),
            args=[app],
            id="data_purge",
            name="Daily expired-user hard purge",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("data_purge scheduler registered (cron=%s)", _DATA_PURGE_CRON)
    except Exception:
        logger.exception("Failed to register data_purge job")
