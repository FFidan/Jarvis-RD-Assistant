"""Zotero library polling: item parsing, DOI linking, ingestion, cursor."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import httpx
from jarvis_common.library import add_to_library
from jarvis_common.paper_state import upsert_paper_user_state as _upsert_paper_user_state
from jarvis_common.task_registry import KIND_TO_TASK
from pydantic import ValidationError

from paper_ingestion.integrations._zotero_config import (
    ZoteroConfigDecryptError,
    _get_zotero_config,
    _resolve_zotero_user_id,
)
from paper_ingestion.models.papers import PaperCreate, SourceType
from paper_ingestion.queries.predicates import paper_visible_sql
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")

# Maximum number of items enqueued per sync cycle.  When this limit is hit the
# library version cursor is NOT advanced so the next sync resumes from the same
# point and processes the next batch.
MAX_ENQUEUE_PER_SYNC = 20


@dataclass(frozen=True, slots=True)
class _PollConfig:
    """Resolved, decrypted Zotero polling config for one cycle."""

    api_key: str
    user_id: str
    library_type: str
    group_id: int | None
    last_version: int


@dataclass(frozen=True, slots=True)
class _ZoteroLibraryNamespace:
    """Identify one remote Zotero library without exposing its credentials.

    Parameters
    ----------
    library_type : {"user", "group"}
        Zotero API library kind.
    remote_id : str
        Zotero user or group identifier for that remote library.

    Raises
    ------
    ValueError
        If the kind or remote identifier is invalid.
    """

    library_type: Literal["user", "group"]
    remote_id: str

    def __post_init__(self) -> None:
        if self.library_type not in {"user", "group"}:
            raise ValueError("Zotero library type must be 'user' or 'group'")
        if not self.remote_id.strip():
            raise ValueError("Zotero remote library ID must not be empty")

    def external_id(self, item_key: str) -> str:
        """Return the canonical paper identity for an item in this library.

        Parameters
        ----------
        item_key : str
            Zotero item key within the remote library.

        Returns
        -------
        str
            Namespace-qualified external paper identifier.

        Raises
        ------
        ValueError
            If ``item_key`` is empty.
        """
        if not item_key.strip():
            raise ValueError("Zotero item key must not be empty")
        return f"zotero:{self.library_type}:{self.remote_id}:{item_key}"


def _namespace_from_poll_config(config: _PollConfig) -> _ZoteroLibraryNamespace:
    """Resolve a validated remote-library namespace from polling config."""
    if config.library_type == "group":
        if config.group_id is None:
            raise ValueError("Zotero group polling requires group_id")
        return _ZoteroLibraryNamespace("group", str(config.group_id))
    if config.library_type == "user":
        return _ZoteroLibraryNamespace("user", config.user_id)
    raise ValueError(f"Unsupported Zotero library type: {config.library_type!r}")


async def _load_poll_config(
    db_pool: asyncpg.Pool, polling_user_id: int | None
) -> _PollConfig | dict[str, str]:
    """Load + validate the Zotero poll config.

    Returns a typed ``_PollConfig`` on success, or an early-status dict
    (``config_decrypt_failed`` / ``disabled`` / ``poll_disabled``) that the
    caller returns verbatim.
    """
    try:
        cfg = await _get_zotero_config(db_pool, user_id=polling_user_id)
    except ZoteroConfigDecryptError:
        logger.warning(
            "Zotero poll: config decryption failed — stored credentials are unreadable "
            "(key rotation?); operator must re-save Zotero API key in Settings"
        )
        return {"status": "config_decrypt_failed"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        logger.warning("Zotero poll: api_key or user_id not configured")
        return {"status": "disabled"}

    if not cfg.get("poll_enabled", False):
        logger.debug("Zotero poll: poll_enabled is false")
        return {"status": "poll_disabled"}

    # Read last known library version (persisted as a JSON number in user_config).
    last_version: int = 0
    raw_version = cfg.get("last_library_version")
    if raw_version is not None:
        last_version = int(raw_version)

    return _PollConfig(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),
        group_id=group_id,
        last_version=last_version,
    )


@dataclass(frozen=True, slots=True)
class _ParsedZoteroItem:
    """Pure projection of a single Zotero API item into ingestion inputs."""

    item_key: str
    doi: str
    authors: list[str]
    url: str
    metadata: dict[str, Any]
    paper_create: PaperCreate


def _parse_zotero_item(
    data: dict[str, Any],
    outer_item: dict[str, Any],
    namespace: _ZoteroLibraryNamespace,
) -> _ParsedZoteroItem | None:
    """Project a Zotero item into namespace-safe ingestion inputs.

    Parameters
    ----------
    data : dict[str, Any]
        Nested Zotero ``item["data"]`` object.
    outer_item : dict[str, Any]
        Top-level Zotero API item used for the item-key fallback.
    namespace : _ZoteroLibraryNamespace
        Remote library that owns the item key.

    Returns
    -------
    _ParsedZoteroItem | None
        Parsed ingestion value, or ``None`` for an item exported by JARVIS.

    Notes
    -----
    Zotero item keys are unique only inside one remote library. The generated
    external ID therefore includes both the library kind and remote ID.
    """
    item_key: str = data.get("key", outer_item.get("key", ""))

    # Skip items that originated in JARVIS.
    extra: str = data.get("extra", "") or ""
    if "jarvis_paper_id=" in extra:
        return None

    doi: str = data.get("DOI", "") or ""

    # Build author list from Zotero creators.
    creators: list[dict[str, str]] = data.get("creators", []) or []
    authors: list[str] = []
    for c in creators:
        first = c.get("firstName", "")
        last = c.get("lastName", "")
        name = f"{first} {last}".strip() if first else last
        if name:
            authors.append(name)

    title: str = data.get("title", "") or ""
    abstract: str = data.get("abstractNote", "") or ""
    url: str = data.get("url", "") or ""
    if not url:
        url = f"https://www.zotero.org/items/{item_key}"
    metadata: dict[str, Any] = {"zotero_item_key": item_key}
    if doi:
        metadata["doi"] = doi

    paper_create = PaperCreate(
        external_id=namespace.external_id(item_key),
        source_type=SourceType.ZOTERO,
        title=title or f"Zotero item {item_key}",
        authors=authors,
        abstract=abstract or None,
        url=url,
        metadata=metadata,
        discovery_origin="user_initiated",
    )

    return _ParsedZoteroItem(
        item_key=item_key,
        doi=doi,
        authors=authors,
        url=url,
        metadata=metadata,
        paper_create=paper_create,
    )


def _safe_parse_zotero_item(
    data: dict[str, Any],
    outer_item: dict[str, Any],
    item_key: str,
    namespace: _ZoteroLibraryNamespace,
) -> _ParsedZoteroItem | None:
    """Call _parse_zotero_item, returning None (and logging) on validation failure.

    Isolates the try/except so the per-item exception branch does not grow
    poll_zotero_library's branch count (PLR0912).
    """
    try:
        return _parse_zotero_item(data, outer_item, namespace)
    except (ValidationError, ValueError):
        logger.warning(
            "Zotero poll: skipping malformed item %s — parse failed",
            item_key,
            exc_info=True,
        )
        return None


async def _link_existing_by_doi(
    db_pool: asyncpg.Pool, doi: str, item_key: str, polling_user_id: int | None
) -> Literal["linked"] | None:
    """DOI deduplication — link to an existing JARVIS paper if one matches.

    Takes the raw ``doi``/``item_key`` (not a parsed item) so the caller can
    resolve the link BEFORE projecting the Zotero item into a PaperCreate model:
    a linked item must never be validated, since a malformed url/over-long title
    on an item that simply matches a paper already in the library would otherwise
    raise and abort the whole poll.

    The de-dup match is visibility-scoped to the syncing user: only rows the
    poller may already read (persisted-public OR present in their own
    ``user_library``) are eligible. A private row owned by another tenant that
    merely shares this DOI is intentionally NOT matched; the caller then falls
    through to ``_ingest_new_item``, which ingests the poller's own
    namespace-qualified copy. This stops a caller-controlled DOI that collides
    with a foreign private row from granting ``user_library`` membership — and
    thus raw-PDF + private-metadata read access — on that row. When the polling
    user is ambiguous (``resolved_polling_user_id`` is ``None``) the membership
    branch matches nothing, so only public rows can link — private never leaks.

    Returns ``"linked"`` when an existing paper was found and linked (the
    caller skips ingestion); ``None`` when no match was found or the lookup
    failed (the caller falls through to ingest a new paper).
    """
    try:
        async with db_pool.acquire() as conn:
            resolved_polling_user_id = await _resolve_zotero_user_id(conn, polling_user_id)
            row = await conn.fetchrow(
                "SELECT p.id, l.zotero_item_key, p.discovered_by FROM papers p"
                " LEFT JOIN paper_user_zotero_links l"
                "   ON l.paper_id = p.id AND l.user_id = $2"
                f" WHERE p.metadata->>'doi' = $1 AND {paper_visible_sql(2)}",
                doi,
                resolved_polling_user_id,
            )
        if row:
            if resolved_polling_user_id is not None and not row["zotero_item_key"] and item_key:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO paper_user_zotero_links
                            (paper_id, user_id, zotero_item_key, updated_at)
                        VALUES ($1, $2, $3, NOW())
                        ON CONFLICT (paper_id, user_id) DO UPDATE
                           SET zotero_item_key = EXCLUDED.zotero_item_key,
                               updated_at = NOW()
                        """,
                        row["id"],
                        resolved_polling_user_id,
                        item_key,
                    )
                try:
                    await KIND_TO_TASK["zotero.sync_annotations"].defer_async(
                        job_id=str(uuid.uuid4()),
                        # Attribute to the syncing user (who triggered this poll).
                        user_id=resolved_polling_user_id,
                        paper_id=row["id"],
                    )
                except Exception:
                    logger.debug(
                        "Zotero poll: failed to enqueue annotation sync for %s",
                        row["id"],
                        exc_info=True,
                    )
            if polling_user_id is not None:
                async with db_pool.acquire() as conn:
                    await add_to_library(
                        conn,
                        user_id=polling_user_id,
                        paper_id=row["id"],
                        added_via="zotero_pull",
                    )
                    # First-sync wins: never overwrite existing user
                    # state (the user may have trashed the paper).
                    await _upsert_paper_user_state(
                        conn,
                        row["id"],
                        polling_user_id,
                        state="to_read",
                        starred=False,
                        on_conflict="do_nothing",
                    )
            return "linked"
    except Exception:
        logger.warning("Zotero poll: DOI lookup failed for key %s", item_key, exc_info=True)
    return None


async def _configured_namespace_for_user(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    user_id: int,
) -> _ZoteroLibraryNamespace | None:
    """Read one linked user's effective remote Zotero library namespace."""
    rows = await conn.fetch(
        """SELECT DISTINCT ON (key) key, value
           FROM user_config
           WHERE key = ANY($2::text[])
             AND (user_id = $1 OR user_id IS NULL)
           ORDER BY key, user_id IS NULL""",
        user_id,
        ["zotero.group_id", "zotero.library_type", "zotero.user_id"],
    )
    values = {str(row["key"]): row["value"] for row in rows}
    library_type = values.get("zotero.library_type", "user")
    remote_id = (
        values.get("zotero.group_id") if library_type == "group" else values.get("zotero.user_id")
    )
    if library_type not in {"user", "group"} or remote_id is None:
        return None
    try:
        return _ZoteroLibraryNamespace(library_type, str(remote_id))
    except ValueError:
        return None


async def _migrate_unambiguous_legacy_identity(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    item_key: str,
    namespace: _ZoteroLibraryNamespace,
) -> None:
    """Rename one legacy Zotero paper only when its remote owner is certain.

    Parameters
    ----------
    conn : asyncpg.Connection | asyncpg.pool.PoolConnectionProxy
        Connection participating in the caller's ingestion transaction.
    item_key : str
        Current Zotero item key.
    namespace : _ZoteroLibraryNamespace
        Remote namespace expected by the current poll.

    Notes
    -----
    Legacy identifiers omitted the remote library. A row is renamed only when
    it has at least one linked user, every linked user resolves to the same
    namespace as the current poll, and the destination is free. Every ambiguous
    case is left private and the normal namespaced upsert creates or reuses the
    correct row.
    """
    legacy_external_id = f"zotero:{item_key}"
    destination_external_id = namespace.external_id(item_key)
    rows = await conn.fetch(
        """SELECT p.id,
                  ARRAY(
                      SELECT l.user_id
                      FROM paper_user_zotero_links l
                      WHERE l.paper_id = p.id
                      ORDER BY l.user_id
                  ) AS linked_user_ids,
                  EXISTS(
                      SELECT 1 FROM papers destination
                      WHERE destination.external_id = $2
                  ) AS destination_exists
           FROM papers p
           WHERE p.external_id = $1
           FOR UPDATE""",
        legacy_external_id,
        destination_external_id,
    )
    if not rows:
        return

    legacy = rows[0]
    linked_user_ids = [int(user_id) for user_id in legacy["linked_user_ids"]]
    if legacy["destination_exists"] or not linked_user_ids:
        logger.warning(
            "Legacy Zotero identity retained because its remote library cannot be migrated safely"
        )
        return

    linked_namespaces = {
        await _configured_namespace_for_user(conn, linked_user_id)
        for linked_user_id in linked_user_ids
    }
    if linked_namespaces != {namespace}:
        logger.warning(
            "Legacy Zotero identity retained because linked remote libraries are ambiguous"
        )
        return

    try:
        # The savepoint contains a possible concurrent unique-key race without
        # aborting the surrounding item-ingestion transaction.
        async with conn.transaction():
            await conn.execute(
                """UPDATE papers
                   SET external_id = $2
                   WHERE id = $1
                     AND external_id = $3
                     AND NOT EXISTS (
                         SELECT 1 FROM papers destination
                         WHERE destination.external_id = $2
                     )""",
                int(legacy["id"]),
                destination_external_id,
                legacy_external_id,
            )
    except asyncpg.UniqueViolationError:
        logger.info("Concurrent Zotero identity migration kept the existing namespaced row")


async def _ingest_new_item(
    db_pool: asyncpg.Pool,
    paper_create: PaperCreate,
    item_key: str,
    polling_user_id: int | None,
    namespace: _ZoteroLibraryNamespace,
) -> bool:
    """Upsert a new paper, mirror it into the polling user's library, store the
    Zotero link, and enqueue ``paper.analyze`` for brand-new papers.

    Returns ``True`` when ``paper.analyze`` was enqueued (the paper was an
    insert), ``False`` otherwise. Raises on DB/enqueue failure so the caller
    can pin the cursor.
    """
    async with db_pool.acquire() as conn, conn.transaction():
        await _migrate_unambiguous_legacy_identity(
            conn,
            item_key=item_key,
            namespace=namespace,
        )
        # Upsert the namespace-qualified private paper, then add exact library
        # membership for the polling user. ``discovered_by`` is audit-only.
        row = await upsert_paper(conn, paper_create, discovered_by=polling_user_id)
        paper_id = row["id"]
        is_new_paper = bool(row["is_insert"])
        if polling_user_id is not None:
            await add_to_library(
                conn,
                user_id=polling_user_id,
                paper_id=paper_id,
                added_via="zotero_pull",
            )
            # First-sync wins: INSERT to_read state but never overwrite
            # existing user state (user may have trashed the paper).
            await _upsert_paper_user_state(
                conn,
                paper_id,
                polling_user_id,
                state="to_read",
                starred=False,
                on_conflict="do_nothing",
            )
        # Store the Zotero item key in the polling user's link row
        # (at-most-once: only when the link's item_key is still NULL).
        if item_key:
            resolved_polling_user_id = await _resolve_zotero_user_id(conn, polling_user_id)
            if resolved_polling_user_id is not None:
                await conn.execute(
                    """
                    INSERT INTO paper_user_zotero_links
                        (paper_id, user_id, zotero_item_key, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (paper_id, user_id) DO UPDATE
                       SET zotero_item_key = EXCLUDED.zotero_item_key,
                           updated_at = NOW()
                     WHERE paper_user_zotero_links.zotero_item_key IS NULL
                    """,
                    paper_id,
                    resolved_polling_user_id,
                    item_key,
                )
    # Enqueue gate = is_insert (brand-new paper), NOT an analysis-
    # completion marker. Zotero-imported papers carry no pdf_url, so
    # _paper_analyze_job raises before the download that would flip
    # pdf_downloaded — gating on pdf_downloaded (or any "analyzed?"
    # field) would re-enqueue every already-imported item on each
    # capped/failed re-poll and pin the cursor (storm). is_insert is
    # False on every re-poll, so the cursor advances to the next batch.
    if is_new_paper:
        await KIND_TO_TASK["paper.analyze"].defer_async(
            job_id=str(uuid.uuid4()),
            user_id=polling_user_id,
            paper_id=paper_id,
        )
        return True
    return False


async def _persist_poll_cursor(
    db_pool: asyncpg.Pool, polling_user_id: int | None, new_version: int
) -> bool:
    """Persist the updated last-library-version cursor.

    Returns ``True`` when the cursor was written, ``False`` when the write
    failed (already logged). The poll itself still succeeded — items were
    processed idempotently and the next poll simply re-reads from the old
    cursor — but the caller must report the cursor as unpersisted rather than
    imply the advance was durable.
    """
    # polling_user_id may be None (single-tenant / system poll). The
    # user_config unique index is NULLS NOT DISTINCT, so the NULL-user row
    # upserts correctly — persist the cursor instead of skipping (skipping
    # left the cursor at 0 and re-polled the whole library forever).
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
            INSERT INTO user_config (user_id, key, value)
            VALUES ($2, 'zotero.last_library_version', $1::jsonb)
            ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
            """,
                new_version,
                polling_user_id,
            )
        return True
    except Exception:
        logger.error("Zotero poll: failed to persist last_library_version", exc_info=True)
        return False


@dataclass(frozen=True, slots=True)
class _PollBatch:
    """Per-cycle counters produced by processing a fetched batch of Zotero items.

    ``parse_failed_keys`` (permanently malformed — will never succeed on retry)
    and ``ingest_failed_keys`` (transient — may succeed on retry) are tracked
    separately so the cursor-pin decision can distinguish them.
    """

    new_count: int
    linked_count: int
    enqueued_count: int
    capped: bool
    parse_failed_keys: list[str]
    ingest_failed_keys: list[str]


async def _process_poll_batch(
    db_pool: asyncpg.Pool,
    items: list[dict[str, Any]],
    polling_user_id: int | None,
    namespace: _ZoteroLibraryNamespace,
) -> _PollBatch:
    """Link or ingest each new item, stopping at the per-cycle enqueue cap."""
    new_count = 0
    linked_count = 0
    enqueued_count = 0
    capped = False  # True when we hit MAX_ENQUEUE_PER_SYNC mid-batch.
    parse_failed_keys: list[str] = []
    ingest_failed_keys: list[str] = []

    for outer_item in items:
        if enqueued_count >= MAX_ENQUEUE_PER_SYNC:
            capped = True
            break
        data: dict[str, Any] = outer_item.get("data", {})
        item_key: str = data.get("key", outer_item.get("key", ""))

        # Skip items that originated in JARVIS.
        if "jarvis_paper_id=" in (data.get("extra", "") or ""):
            continue

        new_count += 1

        # DOI deduplication — resolve the link BEFORE projecting the item into a
        # PaperCreate model, so a linked item is never validated: a malformed
        # url/over-long title on an item that simply matches a paper already in
        # the library must not raise here and abort the whole poll.
        doi: str = data.get("DOI", "") or ""
        if doi and await _link_existing_by_doi(db_pool, doi, item_key, polling_user_id) == "linked":
            linked_count += 1
            continue

        # Not linked → project into ingestion inputs.  Malformed items return
        # None from the safe helper (parse failure logged there) — permanent,
        # so they must not pin the cursor (see poll_zotero_library).
        parsed = _safe_parse_zotero_item(data, outer_item, item_key, namespace)
        if parsed is None:
            parse_failed_keys.append(item_key)
            continue

        try:
            if await _ingest_new_item(
                db_pool,
                parsed.paper_create,
                parsed.item_key,
                polling_user_id,
                namespace,
            ):
                enqueued_count += 1
        except Exception:
            logger.error(
                "Zotero poll: failed to upsert/enqueue paper for key %s",
                parsed.item_key,
                exc_info=True,
            )
            ingest_failed_keys.append(parsed.item_key)

    return _PollBatch(
        new_count, linked_count, enqueued_count, capped, parse_failed_keys, ingest_failed_keys
    )


async def poll_zotero_library(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    polling_user_id: int | None = None,
) -> dict[str, Any]:
    """Incremental poll of Zotero library since last known version.

    For each new item:
    - If Extra field contains 'jarvis_paper_id=' → skip (originated in JARVIS)
    - If DOI matches existing JARVIS paper → link zotero_item_key (skip ingestion)
    - Else → enqueue paper.process job with Zotero metadata as seed

    Persists last library version in user_config as 'zotero.last_library_version'.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    config = await _load_poll_config(db_pool, polling_user_id)
    if isinstance(config, dict):
        return config
    last_version = config.last_version
    try:
        namespace = _namespace_from_poll_config(config)
    except ValueError:
        logger.warning("Zotero poll: remote library identity is incomplete")
        return {"status": "invalid_config"}

    client = ZoteroClient(
        api_key=config.api_key,
        user_id=config.user_id,
        library_type=config.library_type,  # type: ignore[arg-type]
        group_id=config.group_id,
        http_client=http_client,
    )

    try:
        items, new_version = await client.fetch_items_since(last_version)
    except Exception:
        logger.error("Zotero poll: fetch_items_since failed", exc_info=True)
        return {"status": "error", "message": "fetch failed"}

    batch = await _process_poll_batch(db_pool, items, polling_user_id, namespace)

    # Ingest (transient) failures pin the cursor so the next poll retries
    # them from the same starting version. Parse (permanent) failures never
    # succeed on retry, so — when no ingest failure also occurred — they must
    # not poison the cursor forever: log and let it advance past them.
    if batch.ingest_failed_keys:
        logger.error(
            "Zotero poll: %d item(s) failed to ingest; first 5: %s",
            len(batch.ingest_failed_keys),
            batch.ingest_failed_keys[:5],
        )
        new_version = last_version
    elif batch.parse_failed_keys:
        logger.warning(
            "Zotero poll: skipping %d permanently-malformed item(s): %s",
            len(batch.parse_failed_keys),
            batch.parse_failed_keys[:5],
        )

    # Persist updated library version.
    # If the enqueue cap was hit, do NOT advance the cursor — the next sync
    # will re-fetch items starting from last_version and process the next batch.
    if batch.capped:
        new_version = last_version
        logger.info(
            "Zotero poll: enqueue cap (%d) reached — deferring version advance to next sync",
            MAX_ENQUEUE_PER_SYNC,
        )
    cursor_persisted = True
    if new_version != last_version:
        cursor_persisted = await _persist_poll_cursor(db_pool, polling_user_id, new_version)

    logger.info(
        "Zotero poll complete: new=%d linked=%d enqueued=%d version=%d→%d persisted=%s",
        batch.new_count,
        batch.linked_count,
        batch.enqueued_count,
        last_version,
        new_version,
        cursor_persisted,
    )
    return {
        "status": "ok",
        "new_items": batch.new_count,
        "linked": batch.linked_count,
        "enqueued": batch.enqueued_count,
        "version_from": last_version,
        "version_to": new_version,
        "cursor_persisted": cursor_persisted,
    }
