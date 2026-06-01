"""Project Manager business logic layer.

Shared by Telegram handlers and orchestration workflows for project
and task operations.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg

logger = logging.getLogger(__name__)


class ProjectManager:
    """Manages projects and tasks.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    # ----- Projects -----

    async def create_project(
        self,
        name: str,
        *,
        user_id: int | None = None,
        description: str | None = None,
        deadline: datetime | None = None,
    ) -> dict:
        """Create a new project owned by ``user_id`` (NULL for single-tenant owner).

        Parameters
        ----------
        name : str
            Project name.
        user_id : int or None
            Owning user. ``None`` writes a NULL user_id (legacy owner mode).
        description : str or None
            Project description.
        deadline : datetime or None
            Optional deadline.

        Returns
        -------
        dict
            Created project record.
        """
        row = await self.db_pool.fetchrow(
            """INSERT INTO projects (name, description, deadline, user_id)
            VALUES ($1, $2, $3, $4)
            RETURNING *""",
            name,
            description,
            deadline,
            user_id,
        )
        logger.info("Created project: %s (id=%d, user_id=%s)", name, row["id"], user_id)
        return dict(row)

    # ----- Tasks -----

    async def complete_task(self, task_id: int, *, user_id: int | None = None) -> dict:
        """Mark a task as done and update the per-user daily log atomically.

        When ``user_id`` is provided, the UPDATE additionally requires the task
        row's ``user_id`` to match (NULL-safe via ``IS NOT DISTINCT FROM``), so
        a user cannot complete another user's task. ``user_id=None`` preserves
        legacy single-tenant owner semantics (any task is completable).

        Parameters
        ----------
        task_id : int
            Task ID to complete.
        user_id : int or None
            DB user PK to scope ownership; ``None`` = legacy owner mode.

        Returns
        -------
        dict
            Updated task record, or empty dict if not found / not owned.
        """
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE tasks
                    SET status = 'done', completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND status != 'done'
                      AND ($2::bigint IS NULL OR user_id IS NOT DISTINCT FROM $2)
                    RETURNING *""",
                    task_id,
                    user_id,
                )
                if row:
                    today = datetime.now(UTC).date()
                    # daily_log PK is (user_id, log_date) with UNIQUE NULLS NOT
                    # DISTINCT (migration 062) — scope the upsert and increment
                    # so per-user counts don't collide.
                    await conn.execute(
                        "INSERT INTO daily_log (log_date, user_id) VALUES ($1, $2)"
                        " ON CONFLICT (user_id, log_date) DO NOTHING",
                        today,
                        user_id,
                    )
                    await conn.execute(
                        "UPDATE daily_log"
                        " SET tasks_completed = tasks_completed + 1"
                        " WHERE log_date = $1 AND user_id IS NOT DISTINCT FROM $2",
                        today,
                        user_id,
                    )
        return dict(row) if row else {}
