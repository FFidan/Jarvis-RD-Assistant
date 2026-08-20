"""Isolated executor for due, fully acknowledged account erasures."""

from __future__ import annotations

import asyncio
import logging

import asyncpg
from jarvis_common.app_factory import build_database_url
from jarvis_common.config import get_jarvis_common_settings

_EXECUTOR_INTERVAL_SECONDS = 30.0
logger = logging.getLogger(__name__)


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

    Notes
    -----
    The due list is ordered, so a request the finalizer refuses is selected
    first on every pass. Isolating each one keeps a single unfinishable request
    from starving the rest and from restarting this process forever.
    """
    async with pool.acquire() as conn:
        request_ids = await conn.fetch(
            "SELECT request_id FROM platform.due_erasure_request_ids($1)",
            limit,
        )
    finalized = 0
    for row in request_ids:
        request_id = row["request_id"]
        try:
            async with pool.acquire() as conn:
                completed = await conn.fetchval("SELECT platform.finalize_erasure($1)", request_id)
        except asyncpg.PostgresError:
            logger.exception(
                "Account erasure finalization failed",
                extra={"request_id": str(request_id)},
            )
            continue
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
            try:
                await finalize_due_requests(pool)
            except Exception:  # noqa: BLE001
                logger.exception("Account erasure finalization pass failed")
            await asyncio.sleep(_EXECUTOR_INTERVAL_SECONDS)
    finally:
        await pool.close()


def main() -> int:
    """Run the isolated bounded-pass executor until its container stops."""
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
