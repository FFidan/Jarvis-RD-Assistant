"""Qdrant visibility payloads and deployment-wide reconciliation fencing."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import asyncpg
from jarvis_common.paper_visibility import PRIVATE_VISIBILITY_SCOPE, VisibilityScope
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
)

from paper_ingestion.ingestion.embedding_config import COLLECTION_NAME

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

    from paper_ingestion.ingestion.embedder import Embedder

logger = logging.getLogger(__name__)

CHECKPOINT_KEY = "vector_visibility.checkpoint"
CHECKPOINT_VERSION = 1
DEFAULT_BATCH_SIZE = 128
DEFAULT_LEASE_SECONDS = 60
_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_FAIL_CLOSED_GENERATION = "0" * 32
CheckpointStatus = Literal["pending", "complete"]


class StaleVisibilityLeaseError(RuntimeError):
    """Raised when a reconciliation worker no longer owns its generation lease."""


def _validate_generation(generation: str) -> str:
    """Return a normalized generation or raise for malformed input."""
    if not _GENERATION_RE.fullmatch(generation):
        raise ValueError("Vector visibility generation must be 32 lowercase hexadecimal characters")
    return generation


@dataclass(frozen=True, slots=True)
class VectorVisibility:
    """Authorization metadata persisted with one Qdrant point.

    Parameters
    ----------
    source_type : str
        Descriptive source adapter recorded on the canonical paper.
    visibility_scope : {"public", "private"}
        Persisted relational scope. Source labels never grant visibility.
    visibility_generation : str
        Current deployment-wide 32-character hexadecimal generation.
    """

    source_type: str
    visibility_scope: VisibilityScope
    visibility_generation: str

    def __post_init__(self) -> None:
        if not self.source_type.strip():
            raise ValueError("Vector source_type must not be empty")
        if self.visibility_scope not in {"public", "private"}:
            raise ValueError("Vector visibility_scope must be 'public' or 'private'")
        _validate_generation(self.visibility_generation)

    @property
    def payload(self) -> dict[str, str]:
        """Return the authorization fields stored in Qdrant."""
        return {
            "source_type": self.source_type,
            "visibility_scope": self.visibility_scope,
            "visibility_generation": self.visibility_generation,
        }

    @classmethod
    def fail_closed(cls) -> VectorVisibility:
        """Return complete compatibility metadata that cannot match production.

        This value supports isolated unit and maintenance callers that do not
        have a database checkpoint. Production ingestion passes an explicit
        current value; the all-zero generation cannot equal a generated runtime
        checkpoint and therefore under-fetches if accidentally used.
        """
        return cls(
            source_type="unknown",
            visibility_scope=PRIVATE_VISIBILITY_SCOPE,
            visibility_generation=_FAIL_CLOSED_GENERATION,
        )


@dataclass(frozen=True, slots=True)
class VisibilityCheckpoint:
    """Parsed state of the global vector-visibility checkpoint."""

    visibility_generation: str
    status: CheckpointStatus
    last_chunk_id: int
    qdrant_recovery: str | None = None
    rotated_at: str | None = None
    worker_lease_token: str | None = None
    lease_expires_at: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VisibilityCheckpoint:
        """Validate a JSON checkpoint loaded from ``user_config``.

        Parameters
        ----------
        value : Mapping[str, Any]
            Decoded JSONB checkpoint value.

        Returns
        -------
        VisibilityCheckpoint
            Validated immutable checkpoint.

        Raises
        ------
        ValueError
            If the version, generation, status, or progress is malformed.
        """
        if value.get("version") != CHECKPOINT_VERSION:
            raise ValueError("Unsupported vector visibility checkpoint version")
        generation = _validate_generation(str(value.get("visibility_generation", "")))
        status = value.get("status")
        if status not in {"pending", "complete"}:
            raise ValueError("Vector visibility checkpoint status must be pending or complete")
        last_chunk_id = value.get("last_chunk_id")
        if (
            not isinstance(last_chunk_id, int)
            or isinstance(last_chunk_id, bool)
            or last_chunk_id < 0
        ):
            raise ValueError("Vector visibility last_chunk_id must be a non-negative integer")
        return cls(
            visibility_generation=generation,
            status=status,
            last_chunk_id=last_chunk_id,
            qdrant_recovery=_optional_text(value.get("qdrant_recovery")),
            rotated_at=_optional_text(value.get("rotated_at")),
            worker_lease_token=_optional_text(value.get("worker_lease_token")),
            lease_expires_at=_optional_text(value.get("lease_expires_at")),
        )


def _optional_text(value: Any) -> str | None:
    """Return a non-empty string representation or ``None``."""
    return None if value is None or str(value) == "" else str(value)


def _checkpoint_from_row(row: Any) -> VisibilityCheckpoint:
    """Parse a checkpoint from an asyncpg row with a decoded ``value`` field."""
    if row is None or not isinstance(row["value"], Mapping):
        raise RuntimeError("Vector visibility checkpoint is missing or malformed")
    return VisibilityCheckpoint.from_mapping(row["value"])


async def load_visibility_checkpoint(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
) -> VisibilityCheckpoint | None:
    """Load the global checkpoint, returning ``None`` when it is absent."""
    row = await conn.fetchrow(
        "SELECT value FROM user_config WHERE user_id IS NULL AND key = $1",
        CHECKPOINT_KEY,
    )
    return None if row is None else _checkpoint_from_row(row)


async def rotate_visibility_checkpoint(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    generation: str | None = None,
    qdrant_recovery: str = "collection_recreated",
) -> VisibilityCheckpoint:
    """Publish a fresh pending generation and revoke every prior worker lease.

    Parameters
    ----------
    conn : asyncpg.Connection | asyncpg.pool.PoolConnectionProxy
        Connection used for the atomic global ``user_config`` upsert.
    generation : str | None
        Optional deterministic generation for tests; production generates one.
    qdrant_recovery : str
        Non-secret reason recorded for readiness diagnostics.

    Returns
    -------
    VisibilityCheckpoint
        Newly persisted pending checkpoint.
    """
    generation = _validate_generation(generation or secrets.token_hex(16))
    rotated_at = datetime.now(UTC).isoformat()
    row = await conn.fetchrow(
        """INSERT INTO user_config(user_id, key, value)
           VALUES (
               NULL,
               $1,
               jsonb_build_object(
                   'version', 1,
                   'visibility_generation', $2::text,
                   'status', 'pending',
                   'last_chunk_id', 0,
                   'qdrant_recovery', $3::text,
                   'rotated_at', $4::text
               )
           )
           ON CONFLICT (user_id, key) DO UPDATE
           SET value = EXCLUDED.value,
               updated_at = now()
           RETURNING value""",
        CHECKPOINT_KEY,
        generation,
        qdrant_recovery,
        rotated_at,
    )
    return _checkpoint_from_row(row)


async def ensure_visibility_checkpoint(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
) -> VisibilityCheckpoint:
    """Return the existing checkpoint or create an initial pending generation."""
    checkpoint = await load_visibility_checkpoint(conn)
    if checkpoint is not None:
        return checkpoint
    return await rotate_visibility_checkpoint(conn, qdrant_recovery="initial_collection")


async def current_visibility_generation(db_pool: asyncpg.Pool) -> str:
    """Load the current generation from the global checkpoint.

    Raises
    ------
    RuntimeError
        If the checkpoint is absent or malformed. User-facing vector search
        treats that as unavailable rather than running an unscoped query.
    """
    async with db_pool.acquire() as conn:
        checkpoint = await load_visibility_checkpoint(conn)
    if checkpoint is None:
        raise RuntimeError("Vector visibility checkpoint is unavailable")
    return checkpoint.visibility_generation


async def claim_visibility_lease(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    generation: str,
    worker_token: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Claim or renew the exact pending generation when its lease is available."""
    _validate_generation(generation)
    if not worker_token or lease_seconds <= 0:
        raise ValueError("A worker token and positive lease duration are required")
    row = await conn.fetchrow(
        """UPDATE user_config
           SET value = jsonb_set(
                   jsonb_set(value, '{worker_lease_token}', to_jsonb($3::text), true),
                   '{lease_expires_at}',
                   to_jsonb((now() + make_interval(secs => $4))::text),
                   true
               ),
               updated_at = now()
           WHERE user_id IS NULL
             AND key = $1
             AND value->>'visibility_generation' = $2
             AND value->>'status' = 'pending'
             AND (
                 value->>'worker_lease_token' IS NULL
                 OR value->>'worker_lease_token' = $3
                 OR NULLIF(value->>'lease_expires_at', '')::timestamptz <= now()
             )
           RETURNING value""",
        CHECKPOINT_KEY,
        generation,
        worker_token,
        lease_seconds,
    )
    return row is not None


async def visibility_lease_is_current(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    generation: str,
    worker_token: str,
) -> bool:
    """Return whether the exact generation and unexpired worker lease still match."""
    _validate_generation(generation)
    return bool(
        await conn.fetchval(
            """SELECT EXISTS (
                   SELECT 1 FROM user_config
                   WHERE user_id IS NULL
                     AND key = $1
                     AND value->>'visibility_generation' = $2
                     AND value->>'status' = 'pending'
                     AND value->>'worker_lease_token' = $3
                     AND NULLIF(value->>'lease_expires_at', '')::timestamptz > now()
               )""",
            CHECKPOINT_KEY,
            generation,
            worker_token,
        )
    )


async def advance_visibility_checkpoint(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    generation: str,
    worker_token: str,
    last_chunk_id: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Advance progress and renew only the exact generation/lease pair."""
    _validate_generation(generation)
    if last_chunk_id < 0 or lease_seconds <= 0:
        raise ValueError("Checkpoint progress and lease duration must be non-negative")
    row = await conn.fetchrow(
        """UPDATE user_config
           SET value = jsonb_set(
                   jsonb_set(
                       value,
                       '{last_chunk_id}',
                       to_jsonb(GREATEST((value->>'last_chunk_id')::bigint, $4::bigint)),
                       true
                   ),
                   '{lease_expires_at}',
                   to_jsonb((now() + make_interval(secs => $5))::text),
                   true
               ),
               updated_at = now()
           WHERE user_id IS NULL
             AND key = $1
             AND value->>'visibility_generation' = $2
             AND value->>'status' = 'pending'
             AND value->>'worker_lease_token' = $3
             AND NULLIF(value->>'lease_expires_at', '')::timestamptz > now()
           RETURNING value""",
        CHECKPOINT_KEY,
        generation,
        worker_token,
        last_chunk_id,
        lease_seconds,
    )
    return row is not None


async def complete_visibility_checkpoint(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    generation: str,
    worker_token: str,
) -> bool:
    """Mark only the exact current generation/lease pair complete."""
    _validate_generation(generation)
    row = await conn.fetchrow(
        """UPDATE user_config
           SET value = jsonb_set(
                   value - 'worker_lease_token' - 'lease_expires_at',
                   '{status}',
                   to_jsonb('complete'::text),
                   true
               ),
               updated_at = now()
           WHERE user_id IS NULL
             AND key = $1
             AND value->>'visibility_generation' = $2
             AND value->>'status' = 'pending'
             AND value->>'worker_lease_token' = $3
             AND NULLIF(value->>'lease_expires_at', '')::timestamptz > now()
           RETURNING value""",
        CHECKPOINT_KEY,
        generation,
        worker_token,
    )
    return row is not None


async def ensure_visibility_payload_indexes(
    qdrant: AsyncQdrantClient,
    *,
    collection_name: str = COLLECTION_NAME,
) -> None:
    """Create idempotent authorization indexes on the selected collection."""
    from qdrant_client.models import PayloadSchemaType  # noqa: PLC0415

    schemas = {
        "paper_id": PayloadSchemaType.INTEGER,
        "source_type": PayloadSchemaType.KEYWORD,
        "visibility_scope": PayloadSchemaType.KEYWORD,
        "visibility_generation": PayloadSchemaType.KEYWORD,
    }
    for field_name, field_schema in schemas.items():
        await qdrant.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
            wait=True,
        )


async def validate_checkpoint_collection_pair(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    qdrant: AsyncQdrantClient,
    checkpoint: VisibilityCheckpoint,
    *,
    collection_name: str = COLLECTION_NAME,
) -> VisibilityCheckpoint:
    """Rotate a copied complete checkpoint that cannot certify this collection.

    A completed checkpoint is accepted when PostgreSQL has no chunks or Qdrant
    contains at least one point from that exact generation. If PostgreSQL has
    chunks but the collection has none from the generation, retrieval already
    under-fetches; rotation makes that degraded state explicit and starts repair.
    """
    if checkpoint.status != "complete":
        return checkpoint
    chunk_count = int(await conn.fetchval("SELECT COUNT(*) FROM paper_chunks") or 0)
    if chunk_count == 0:
        return checkpoint
    count = await qdrant.count(
        collection_name=collection_name,
        count_filter=Filter(
            must=[
                FieldCondition(
                    key="visibility_generation",
                    match=MatchValue(value=checkpoint.visibility_generation),
                )
            ]
        ),
        exact=True,
    )
    if int(count.count) > 0:
        return checkpoint
    return await rotate_visibility_checkpoint(conn, qdrant_recovery="collection_mismatch")


async def prepare_visibility_schema(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    qdrant: AsyncQdrantClient,
    *,
    collection_created: bool,
    collection_name: str = COLLECTION_NAME,
) -> VisibilityCheckpoint:
    """Initialize indexes and a checkpoint after Qdrant collection setup."""
    await ensure_visibility_payload_indexes(qdrant, collection_name=collection_name)
    checkpoint = (
        await rotate_visibility_checkpoint(conn, qdrant_recovery="collection_created")
        if collection_created
        else await ensure_visibility_checkpoint(conn)
    )
    return await validate_checkpoint_collection_pair(
        conn,
        qdrant,
        checkpoint,
        collection_name=collection_name,
    )


async def reconcile_visibility_payloads(
    db_pool: asyncpg.Pool,
    embedder: Embedder,
    checkpoint: VisibilityCheckpoint,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    worker_token: str | None = None,
) -> Literal["busy", "complete", "stale"]:
    """Repair all persisted papers under a generation-and-lease CAS fence.

    The cross-paper worker owns only keyset progress and lease fencing. Actual
    point identity checks, payload-only repair, guarded delete, and re-embedding
    remain in the existing per-paper reconciliation path.
    """
    if checkpoint.status == "complete":
        return "complete"
    if batch_size <= 0:
        raise ValueError("Visibility reconciliation batch_size must be positive")
    generation = checkpoint.visibility_generation
    token = worker_token or secrets.token_hex(16)
    async with db_pool.acquire() as conn:
        claimed = await claim_visibility_lease(
            conn,
            generation=generation,
            worker_token=token,
        )
    if not claimed:
        return "busy"

    last_chunk_id = checkpoint.last_chunk_id
    while True:
        async with db_pool.acquire() as conn:
            if not await visibility_lease_is_current(
                conn,
                generation=generation,
                worker_token=token,
            ):
                return "stale"
            rows = await conn.fetch(
                """SELECT id, paper_id
                   FROM paper_chunks
                   WHERE id > $1
                   ORDER BY id
                   LIMIT $2""",
                last_chunk_id,
                batch_size,
            )
        if not rows:
            async with db_pool.acquire() as conn:
                completed = await complete_visibility_checkpoint(
                    conn,
                    generation=generation,
                    worker_token=token,
                )
            return "complete" if completed else "stale"

        from paper_ingestion.services.pdf_workflow import (  # noqa: PLC0415
            reconcile_paper_embeddings,
        )

        paper_ids = list(dict.fromkeys(int(row["paper_id"]) for row in rows))
        for paper_id in paper_ids:
            try:
                await reconcile_paper_embeddings(
                    paper_id,
                    db_pool,
                    embedder,
                    visibility_generation=generation,
                    worker_lease_token=token,
                )
            except StaleVisibilityLeaseError:
                return "stale"
        last_chunk_id = int(rows[-1]["id"])
        async with db_pool.acquire() as conn:
            advanced = await advance_visibility_checkpoint(
                conn,
                generation=generation,
                worker_token=token,
                last_chunk_id=last_chunk_id,
            )
        if not advanced:
            return "stale"


async def run_visibility_reconciler(
    db_pool: asyncpg.Pool,
    embedder: Embedder,
    *,
    retry_initial_seconds: float = 1.0,
    retry_max_seconds: float = 30.0,
) -> None:
    """Retry pending visibility repair until completion or task cancellation."""
    delay = retry_initial_seconds
    while True:
        try:
            async with db_pool.acquire() as conn:
                checkpoint = await ensure_visibility_checkpoint(conn)
            result = await reconcile_visibility_payloads(db_pool, embedder, checkpoint)
            if result == "complete":
                return
            delay = retry_initial_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Vector visibility reconciliation failed; retrying")
            delay = min(max(delay * 2, retry_initial_seconds), retry_max_seconds)
        await asyncio.sleep(delay)


async def visibility_checkpoint_progress(db_pool: asyncpg.Pool) -> dict[str, Any]:
    """Return bounded readiness metadata for the current generation."""
    async with db_pool.acquire() as conn:
        checkpoint = await load_visibility_checkpoint(conn)
        total = int(await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM paper_chunks") or 0)
    if checkpoint is None:
        return {"status": "missing", "last_chunk_id": 0, "total_chunk_id": total}
    return {
        "status": checkpoint.status,
        "visibility_generation": checkpoint.visibility_generation,
        "last_chunk_id": checkpoint.last_chunk_id,
        "total_chunk_id": total,
        "qdrant_recovery": checkpoint.qdrant_recovery,
    }
