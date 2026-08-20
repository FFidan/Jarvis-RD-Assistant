"""One-shot database migration and read-only schema-check commands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Sequence

import asyncpg

from jarvis_common.app_factory import build_database_url
from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.migrations import check_migrations, run_migrations

logger = logging.getLogger(__name__)


def _database_url() -> str:
    """Build the migrator DSN from its explicit credential configuration."""
    settings = get_jarvis_common_settings()
    return build_database_url(
        user=settings.postgres_user,
        password_file=settings.postgres_password_file,
    )


async def _run(command: str) -> int:
    """Execute one migration command and emit non-secret lifecycle records."""
    started = time.monotonic()
    logger.info("migration_cli event=start command=%s", command)
    pool: asyncpg.Pool | None = None
    try:
        pool = await asyncpg.create_pool(_database_url())
        if command == "migrate":
            await run_migrations(pool)
        else:
            await check_migrations(pool)
    except Exception as exc:  # noqa: BLE001 - command boundary reports one safe failure record
        error_summary = str(exc).splitlines()[0][:240]
        logger.error(
            "migration_cli event=failure command=%s error_type=%s error=%r duration_ms=%d",
            command,
            type(exc).__name__,
            error_summary,
            int((time.monotonic() - started) * 1000),
        )
        return 1
    finally:
        if pool is not None:
            await pool.close()

    logger.info(
        "migration_cli event=success command=%s duration_ms=%d",
        command,
        int((time.monotonic() - started) * 1000),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the one-shot migrator or the read-only runtime schema check.

    Parameters
    ----------
    argv:
        Optional command-line arguments excluding the executable name.

    Returns
    -------
    int
        Zero on success and one when migration or integrity validation fails.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "check"))
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.command))


if __name__ == "__main__":
    raise SystemExit(main())
