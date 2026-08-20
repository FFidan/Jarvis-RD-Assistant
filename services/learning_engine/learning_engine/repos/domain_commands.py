"""Idempotent Learning command persistence and application."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

import asyncpg

CommandType = Literal[
    "paper.read", "paper.deleted", "project.zotero_collection", "journal.upsert", "user.erase"
]


async def apply_command(
    pool: asyncpg.Pool,
    *,
    command_type: CommandType,
    request_id: str,
    user_id: int,
    payload: dict[str, Any],
) -> bool:
    """Apply one exact command once and return whether this call performed work."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO domain_commands (
                    id, command_type, request_id, user_id, paper_id, payload
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (command_type, request_id) DO NOTHING
                RETURNING TRUE
                """,
                uuid.uuid4(),
                command_type,
                request_id,
                user_id,
                payload.get("paper_id"),
                payload,
            )
            if inserted is not True:
                if command_type == "user.erase":
                    # Successful erasure deliberately scrubs its inbox row's
                    # subject and payload. The route-bound request UUID remains
                    # the idempotency proof for a duplicate executor call.
                    return False
                existing = await conn.fetchrow(
                    """SELECT user_id, paper_id, payload FROM domain_commands
                       WHERE command_type = $1 AND request_id = $2""",
                    command_type,
                    request_id,
                )
                if (
                    existing is None
                    or existing["user_id"] != user_id
                    or existing["paper_id"] != payload.get("paper_id")
                    or existing["payload"] != payload
                ):
                    raise ValueError("idempotency key was reused with a different command")
                return False
            if command_type == "paper.read":
                await conn.execute(
                    """
                    INSERT INTO daily_log (user_id, log_date, papers_read)
                    VALUES ($1, CURRENT_DATE, 1)
                    ON CONFLICT (user_id, log_date)
                    DO UPDATE SET papers_read = COALESCE(daily_log.papers_read, 0) + 1
                    """,
                    user_id,
                )
            elif command_type == "paper.deleted":
                paper_id = int(payload["paper_id"])
                await conn.execute(
                    "DELETE FROM cards WHERE paper_id = $1 AND user_id = $2", paper_id, user_id
                )
                await conn.execute(
                    """
                    DELETE FROM task_paper_links AS link USING tasks AS task
                    WHERE link.task_id = task.id AND link.paper_id = $1 AND task.user_id = $2
                    """,
                    paper_id,
                    user_id,
                )
                await conn.execute(
                    """
                    DELETE FROM project_papers AS link USING projects AS project
                    WHERE link.project_id = project.id
                      AND link.paper_id = $1
                      AND project.user_id = $2
                    """,
                    paper_id,
                    user_id,
                )
            elif command_type == "project.zotero_collection":
                updated = await conn.execute(
                    """
                    UPDATE projects SET zotero_collection_key = $1, updated_at = NOW()
                    WHERE id = $2 AND user_id = $3
                    """,
                    str(payload["zotero_collection_key"]),
                    int(payload["project_id"]),
                    user_id,
                )
                if updated != "UPDATE 1":
                    raise RuntimeError("Learning project is unavailable for this user")
            elif command_type == "journal.upsert":
                # The payload stays JSON-primitive so the inbox row encodes and a
                # duplicate delivery compares equal; the day is rebuilt at the bind.
                await conn.execute(
                    """
                    INSERT INTO journal_entries (user_id, date, prompts, updated_at)
                    VALUES ($1, $2, $3::jsonb, NOW())
                    ON CONFLICT ON CONSTRAINT journal_entries_user_id_date_key
                    DO UPDATE SET prompts = EXCLUDED.prompts, updated_at = NOW()
                    """,
                    user_id,
                    date.fromisoformat(payload["date"]),
                    payload["prompts"],
                )
            else:
                await conn.execute(
                    "SELECT learning.erase_user_data($1, $2)",
                    user_id,
                    request_id,
                )
            await conn.execute(
                """UPDATE domain_commands
                   SET processed_at = NOW(), acknowledgement_at = NOW(), last_error = NULL
                   WHERE command_type = $1 AND request_id = $2""",
                command_type,
                request_id,
            )
    return True


__all__ = ["CommandType", "apply_command"]
