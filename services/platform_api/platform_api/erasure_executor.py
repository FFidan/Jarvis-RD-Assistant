"""Isolated executor for due, fully acknowledged account erasures."""

from __future__ import annotations

import asyncio

import asyncpg
from jarvis_common.app_factory import build_database_url
from jarvis_common.config import get_jarvis_common_settings

_EXECUTOR_INTERVAL_SECONDS = 30.0


async def finalize_due_requests(pool: asyncpg.Pool, *, limit: int = 20) -> int:
    """Finalize a bounded pass of due erasure requests.

    Parameters
    ----------
    pool : asyncpg.Pool
        Pool authenticated as the isolated erasure executor.
    limit : int, default=20
        Maximum number of due requests to process in this pass.

    Returns
    -------
    int
        Number of requests whose idempotent finalization completed.
    """
    async with pool.acquire() as conn:
        request_ids = await conn.fetch(
            "SELECT request_id FROM platform.due_erasure_request_ids($1)",
            limit,
        )
    finalized = 0
    for row in request_ids:
        async with pool.acquire() as conn:
            completed = await conn.fetchval(
                "SELECT platform.finalize_erasure($1)", row["request_id"]
            )
        finalized += int(completed is True)
    return finalized


async def _run() -> None:
    settings = get_jarvis_common_settings()
    pool = await asyncpg.create_pool(
        build_database_url(
            user=settings.postgres_user, password_file=settings.postgres_password_file
        ),
        min_size=1,
        max_size=2,
    )
    try:
        while True:
            await finalize_due_requests(pool)
            await asyncio.sleep(_EXECUTOR_INTERVAL_SECONDS)
    finally:
        await pool.close()


def main() -> int:
    """Run the isolated bounded-pass executor until its container stops."""
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
