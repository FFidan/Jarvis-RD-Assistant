"""Project Manager business logic layer.

Shared by Telegram handlers and orchestration workflows for project,
task, milestone, and daily log operations.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg
from jarvis_common import quote_ident

logger = logging.getLogger(__name__)


class ProjectManager:
    """Manages projects, tasks, milestones, and daily logs.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    # ----- Projects -----

    async def list_projects(self, status: str = "active") -> list[dict]:
        """List projects filtered by status.

        Parameters
        ----------
        status : str
            Project status filter (default ``'active'``).

        Returns
        -------
        list[dict]
            List of project records.
        """
        rows = await self.db_pool.fetch(
            "SELECT * FROM projects WHERE status = $1 ORDER BY created_at DESC",
            status,
        )
        return [dict(r) for r in rows]

    async def create_project(
        self,
        name: str,
        description: str | None = None,
        deadline: datetime | None = None,
    ) -> dict:
        """Create a new project.

        Parameters
        ----------
        name : str
            Project name.
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
            """INSERT INTO projects (name, description, deadline)
            VALUES ($1, $2, $3)
            RETURNING *""",
            name,
            description,
            deadline,
        )
        logger.info("Created project: %s (id=%d)", name, row["id"])
        return dict(row)

    VALID_PROJECT_STATUSES = {"active", "paused", "completed", "archived"}

    async def update_project_status(self, project_id: int, status: str) -> dict:
        """Update a project's status.

        Parameters
        ----------
        project_id : int
            Project ID.
        status : str
            New status (``active``, ``paused``, ``completed``, ``archived``).

        Returns
        -------
        dict
            Updated project record, or empty dict if not found.

        Raises
        ------
        ValueError
            If status is not a valid project status.
        """
        if status not in self.VALID_PROJECT_STATUSES:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {sorted(self.VALID_PROJECT_STATUSES)}"
            )
        row = await self.db_pool.fetchrow(
            """UPDATE projects SET status = $1, updated_at = NOW()
            WHERE id = $2 RETURNING *""",
            status,
            project_id,
        )
        return dict(row) if row else {}

    # ----- Tasks -----

    async def list_tasks(
        self,
        project_id: int | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List tasks with optional filters.

        Parameters
        ----------
        project_id : int or None
            Filter by project.
        status : str or None
            Filter by status.

        Returns
        -------
        list[dict]
            List of task records with project name.
        """
        conditions: list[str] = []
        params: list[object] = []
        idx = 1

        if project_id is not None:
            conditions.append(f"t.project_id = ${idx}")
            params.append(project_id)
            idx += 1
        if status is not None:
            conditions.append(f"t.status = ${idx}")
            params.append(status)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = await self.db_pool.fetch(
            f"""SELECT t.*, p.name as project_name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            {where}
            ORDER BY t.priority, t.deadline NULLS LAST""",  # nosec B608 - WHERE clause is assembled from fixed column snippets, values stay parameterized
            *params,
        )
        return [dict(r) for r in rows]

    async def create_task(
        self,
        project_id: int,
        title: str,
        priority: int = 3,
        deadline: datetime | None = None,
    ) -> dict:
        """Create a new task.

        Parameters
        ----------
        project_id : int
            Parent project ID.
        title : str
            Task title.
        priority : int
            Priority (1=critical, 2=high, 3=medium, 4=low).
        deadline : datetime or None
            Optional deadline.

        Returns
        -------
        dict
            Created task record.
        """
        row = await self.db_pool.fetchrow(
            """INSERT INTO tasks (project_id, title, priority, deadline)
            VALUES ($1, $2, $3, $4)
            RETURNING *""",
            project_id,
            title,
            priority,
            deadline,
        )
        logger.info("Created task: %s (id=%d)", title, row["id"])
        return dict(row)

    async def complete_task(self, task_id: int) -> dict:
        """Mark a task as done and update daily log atomically.

        Parameters
        ----------
        task_id : int
            Task ID to complete.

        Returns
        -------
        dict
            Updated task record, or empty dict if not found.
        """
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE tasks
                    SET status = 'done', completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND status != 'done'
                    RETURNING *""",
                    task_id,
                )
                if row:
                    today = datetime.now(UTC).date()
                    await conn.execute(
                        "INSERT INTO daily_log (log_date) VALUES ($1)"
                        " ON CONFLICT (log_date) DO NOTHING",
                        today,
                    )
                    await conn.execute(
                        "UPDATE daily_log"
                        " SET tasks_completed = tasks_completed + 1"
                        " WHERE log_date = $1",
                        today,
                    )
        return dict(row) if row else {}

    async def get_today_tasks(self) -> list[dict]:
        """Get tasks that are in progress or due today.

        Returns
        -------
        list[dict]
            List of task records with project name.
        """
        rows = await self.db_pool.fetch(
            """SELECT t.*, p.name as project_name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'in_progress'
               OR (t.deadline IS NOT NULL
                   AND t.deadline::date = CURRENT_DATE
                   AND t.status != 'done')
            ORDER BY t.priority, t.deadline NULLS LAST"""
        )
        return [dict(r) for r in rows]

    # ----- Milestones -----

    async def get_upcoming_milestones(self, days: int = 3) -> list[dict]:
        """Get milestones due in the next *N* days.

        Parameters
        ----------
        days : int
            Number of days to look ahead (default 3).

        Returns
        -------
        list[dict]
            List of milestone records with project name.
        """
        rows = await self.db_pool.fetch(
            """SELECT m.*, p.name as project_name
            FROM milestones m
            JOIN projects p ON m.project_id = p.id
            WHERE m.completed = FALSE
              AND m.deadline <= NOW() + make_interval(days => $1)
            ORDER BY m.deadline""",
            days,
        )
        return [dict(r) for r in rows]

    async def complete_milestone(self, milestone_id: int) -> dict:
        """Mark a milestone as completed.

        Parameters
        ----------
        milestone_id : int
            Milestone ID.

        Returns
        -------
        dict
            Updated milestone record, or empty dict if not found.
        """
        row = await self.db_pool.fetchrow(
            """UPDATE milestones
            SET completed = TRUE, completed_at = NOW()
            WHERE id = $1
            RETURNING *""",
            milestone_id,
        )
        return dict(row) if row else {}

    # ----- Paper Links -----

    async def link_paper_to_task(
        self, task_id: int, paper_id: int, note: str | None = None
    ) -> None:
        """Link a paper to a task.

        Parameters
        ----------
        task_id : int
            Task ID.
        paper_id : int
            Paper ID.
        note : str or None
            Optional note about the link.
        """
        await self.db_pool.execute(
            """INSERT INTO task_paper_links (task_id, paper_id, note)
            VALUES ($1, $2, $3)
            ON CONFLICT (task_id, paper_id) DO UPDATE SET note = $3""",
            task_id,
            paper_id,
            note,
        )

    async def get_project_papers(self, project_id: int) -> list[dict]:
        """Get all papers linked to a project's tasks.

        Parameters
        ----------
        project_id : int
            Project ID.

        Returns
        -------
        list[dict]
            List of paper records with task info.
        """
        rows = await self.db_pool.fetch(
            """SELECT p.*, tpl.note, t.title as task_title
            FROM task_paper_links tpl
            JOIN papers p ON tpl.paper_id = p.id
            JOIN tasks t ON tpl.task_id = t.id
            WHERE t.project_id = $1
            ORDER BY p.created_at DESC""",
            project_id,
        )
        return [dict(r) for r in rows]

    # ----- Daily Log -----

    async def update_daily_log(self, **increments: int) -> dict:
        """Upsert today's daily log entry with incremental updates.

        Parameters
        ----------
        **increments : int
            Fields to increment (``tasks_completed``, ``cards_reviewed``,
            ``papers_read``).

        Returns
        -------
        dict
            Updated daily log record, or empty dict on failure.
        """
        today = datetime.now(UTC).date()

        # Ensure row exists
        await self.db_pool.execute(
            "INSERT INTO daily_log (log_date) VALUES ($1) ON CONFLICT (log_date) DO NOTHING",
            today,
        )

        # Apply increments (field names are from a hardcoded allowlist, never user input)
        _allowed_log_fields = frozenset({"tasks_completed", "cards_reviewed", "papers_read"})
        for field, value in increments.items():
            if field not in _allowed_log_fields:
                raise ValueError(f"Disallowed field: {field!r}")
            if value:
                qf = quote_ident(field)
                await self.db_pool.execute(
                    f"UPDATE daily_log SET {qf} = {qf} + $1 WHERE log_date = $2",  # nosec B608 - field is selected from an allowlist and values stay parameterized
                    value,
                    today,
                )

        row = await self.db_pool.fetchrow(
            "SELECT * FROM daily_log WHERE log_date = $1", today
        )
        return dict(row) if row else {}
