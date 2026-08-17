"""Boot-time hardware re-probe — writes system_events row on tier change."""

from __future__ import annotations

import logging
import os

import asyncpg
from jarvis_common.hw_detect import detect_tier

logger = logging.getLogger(__name__)


async def run_boot_probe(pool: asyncpg.Pool) -> None:
    baseline = os.getenv("JARVIS_HW_TIER")
    current = detect_tier()
    if not baseline:
        logger.info("hw_probe: no baseline in env; skipping")
        return
    if baseline == current:
        logger.info("hw_probe: tier unchanged (%s)", current)
        return
    logger.warning("hw_probe: tier changed %s -> %s", baseline, current)
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT platform.append_system_event_v1($1, $2, $3, $4, $5::jsonb, NULL)",
            "warning",
            "infra",
            "hw_probe",
            f"hw tier changed: {baseline} -> {current}",
            {"from": baseline, "to": current},
        )
