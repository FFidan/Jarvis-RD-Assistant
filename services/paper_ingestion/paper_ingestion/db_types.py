"""Shared database type aliases for paper_ingestion."""

import asyncpg

ConnLike = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]
