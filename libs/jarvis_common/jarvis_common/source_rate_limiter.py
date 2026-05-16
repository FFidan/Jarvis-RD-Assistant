"""Token-bucket rate limiter for source plugins.

Provides :class:`SourceRateLimiter` — a simple asyncio-compatible token-bucket
that caps the request rate for external API sources (arXiv, Semantic Scholar,
OpenAlex, PubMed, …).

Also provides :class:`PersistentSourceRateLimiter` — a Postgres-backed variant
that coordinates across processes using the ``source_health`` table, with
transparent fallback to an in-memory limiter when the DB is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

_logger = logging.getLogger(__name__)
_DEFAULT_COOLDOWN_MINUTES = 60


class SourceRateLimiter:
    """Token-bucket rate limiter for source plugins.

    Parameters
    ----------
    rate_per_second:
        Sustained request rate (tokens refilled per second).
    burst:
        Maximum token bucket capacity (allows short bursts above the sustained
        rate). Defaults to 1 (no bursting).

    Note: also accepts ``requests_per_minute`` as a convenience alias
    (converted to rate_per_second internally).
    """

    def __init__(
        self,
        rate_per_second: float = 0.0,
        burst: int = 1,
        *,
        requests_per_minute: float | None = None,
    ) -> None:
        if requests_per_minute is not None:
            rate_per_second = requests_per_minute / 60.0
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self.rate = rate_per_second
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire one token, sleeping if the bucket is empty."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                # Reset after sleep: the sleep satisfied the wait, so the bucket
                # starts fresh. Without this, the next refill would count the
                # sleep duration as elapsed time and over-fill the bucket.
                self.last_refill = time.monotonic()
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


class PersistentSourceRateLimiter:
    """Postgres-backed rate limiter using the ``source_health`` table.

    Coordinates across processes; falls back to the in-memory
    :class:`SourceRateLimiter` (or a bare ``asyncio.sleep``) on DB outage.

    Parameters
    ----------
    source_type:
        Matches ``source_health.source_type`` (e.g. ``"arxiv"``).
    user_id:
        Matches ``source_health.user_id``; ``None`` for instance-wide rows.
    min_interval_seconds:
        Minimum elapsed seconds between consecutive requests.
    db_pool:
        asyncpg connection pool shared with the application.
    fallback:
        Optional in-memory :class:`SourceRateLimiter` used when Postgres is
        unreachable.  If omitted, a bare ``asyncio.sleep(min_interval_seconds)``
        is used instead.
    """

    def __init__(
        self,
        source_type: str,
        user_id: int | None,
        min_interval_seconds: float,
        db_pool: Any,
        fallback: SourceRateLimiter | None = None,
    ) -> None:
        self._source_type = source_type
        self._user_id = user_id
        self._min_interval = min_interval_seconds
        self._pool = db_pool
        self._fallback = fallback

    async def acquire(self) -> None:
        """Wait until it is safe to issue the next request.

        Atomically claims the rate-limit slot in ``source_health`` using an
        ``INSERT … ON CONFLICT DO UPDATE … WHERE … RETURNING`` statement.
        Only one concurrent worker can claim the slot per ``min_interval_seconds``;
        the other(s) read the current ``last_request_at``, sleep for the
        remaining interval, and retry the claim once.

        Also honours ``cooldown_until`` (hard API cooldown) — both workers sleep.

        Falls back to the in-memory fallback (or a bare sleep) on DB error.
        """
        try:
            await self._acquire_with_retry()
        except Exception as exc:
            _logger.warning(
                "PersistentSourceRateLimiter[%s] DB acquire failed (%s); using fallback",
                self._source_type,
                exc,
                exc_info=True,
            )
            if self._fallback is not None:
                await self._fallback.acquire()
            else:
                await asyncio.sleep(self._min_interval)

    async def _acquire_with_retry(self) -> None:
        """Inner acquire: check cooldown, then atomic slot claim (one retry)."""
        # ── 1. Cooldown check (read-only; both workers should respect this) ──
        async with self._pool.acquire() as conn:
            cooldown_row = await conn.fetchrow(
                """
                SELECT cooldown_until
                  FROM source_health
                 WHERE (user_id = $1 OR ($1 IS NULL AND user_id IS NULL))
                   AND source_type = $2
                """,
                self._user_id,
                self._source_type,
            )
        if cooldown_row is not None:
            cooldown_until: datetime | None = cooldown_row["cooldown_until"]
            if cooldown_until is not None:
                now = datetime.now(tz=UTC)
                if cooldown_until > now:
                    wait = (cooldown_until - now).total_seconds()
                    _logger.debug(
                        "PersistentSourceRateLimiter[%s] in cooldown; sleeping %.1fs",
                        self._source_type,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    return

        # ── 2. Atomic slot claim (up to 2 attempts) ──────────────────────────
        # The INSERT path handles the first-ever request for this source (no
        # existing row); ON CONFLICT targets the UNIQUE(user_id, source_type)
        # constraint.  The WHERE clause on DO UPDATE makes the update
        # conditional: it only fires (and returns a row) when the slot is free.
        # A concurrent worker that already updated last_request_at within the
        # interval will cause the WHERE to be false → no row returned → slot
        # is taken.
        for attempt in range(2):
            async with self._pool.acquire() as conn:
                claimed_row = await conn.fetchrow(
                    """
                    INSERT INTO source_health
                        (user_id, source_type, last_request_at, updated_at)
                    VALUES ($1, $2, now(), now())
                    ON CONFLICT (user_id, source_type) DO UPDATE
                       SET last_request_at = now(),
                           updated_at      = now()
                     WHERE source_health.last_request_at IS NULL
                        OR source_health.last_request_at
                               < now() - ($3 || ' seconds')::interval
                    RETURNING last_request_at
                    """,
                    self._user_id,
                    self._source_type,
                    str(self._min_interval),
                )

            if claimed_row is not None:
                # Slot claimed — proceed immediately.
                return

            if attempt == 0:
                # Slot taken by another worker; read its last_request_at and
                # sleep for the remaining interval before retrying.
                async with self._pool.acquire() as conn:
                    wait_row = await conn.fetchrow(
                        """
                        SELECT last_request_at
                          FROM source_health
                         WHERE (user_id = $1 OR ($1 IS NULL AND user_id IS NULL))
                           AND source_type = $2
                        """,
                        self._user_id,
                        self._source_type,
                    )
                if wait_row is not None and wait_row["last_request_at"] is not None:
                    elapsed = (datetime.now(tz=UTC) - wait_row["last_request_at"]).total_seconds()
                    wait = max(0.0, self._min_interval - elapsed)
                else:
                    wait = self._min_interval
                _logger.debug(
                    "PersistentSourceRateLimiter[%s] slot taken; sleeping %.1fs",
                    self._source_type,
                    wait,
                )
                await asyncio.sleep(wait)
            # attempt == 1: second claim attempt after sleeping; if it also
            # fails (unlikely), fall through and return — don't block forever.

    async def update_last_request(
        self,
        status: Literal["ok", "rate_limit", "error"],
        retry_after_s: int | None = None,
    ) -> None:
        """Persist the outcome of the most recent HTTP request.

        * ``"ok"``: clears ``cooldown_until``, resets ``consecutive_failures``.
        * ``"rate_limit"``: sets ``cooldown_until = NOW() + retry_after_s``.
        * ``"error"``: increments ``consecutive_failures``.
        """
        now = datetime.now(tz=UTC)
        try:
            async with self._pool.acquire() as conn:
                if status == "ok":
                    await conn.execute(
                        """
                        INSERT INTO source_health
                            (user_id, source_type, last_request_at, last_status,
                             cooldown_until, consecutive_failures, updated_at)
                        VALUES ($1, $2, $3, $4, NULL, 0, $3)
                        ON CONFLICT (user_id, source_type) DO UPDATE
                           SET last_request_at = EXCLUDED.last_request_at,
                               last_status = EXCLUDED.last_status,
                               updated_at = EXCLUDED.updated_at,
                               cooldown_until = NULL,
                               consecutive_failures = 0
                        """,
                        self._user_id,
                        self._source_type,
                        now,
                        status,
                    )
                elif status == "rate_limit":
                    cooldown_s = (
                        retry_after_s
                        if retry_after_s is not None
                        else _DEFAULT_COOLDOWN_MINUTES * 60
                    )
                    cooldown_dt = now + timedelta(seconds=cooldown_s)
                    await conn.execute(
                        """
                        INSERT INTO source_health
                            (user_id, source_type, last_request_at, last_status,
                             cooldown_until, consecutive_failures, updated_at)
                        VALUES ($1, $2, $3, $4, $5, 0, $3)
                        ON CONFLICT (user_id, source_type) DO UPDATE
                           SET last_request_at = EXCLUDED.last_request_at,
                               last_status = EXCLUDED.last_status,
                               updated_at = EXCLUDED.updated_at,
                               cooldown_until = EXCLUDED.cooldown_until
                        """,
                        self._user_id,
                        self._source_type,
                        now,
                        status,
                        cooldown_dt,
                    )
                else:  # "error"
                    await conn.execute(
                        """
                        INSERT INTO source_health
                            (user_id, source_type, last_request_at, last_status,
                             consecutive_failures, updated_at)
                        VALUES ($1, $2, $3, $4, 1, $3)
                        ON CONFLICT (user_id, source_type) DO UPDATE
                           SET last_request_at = EXCLUDED.last_request_at,
                               last_status = EXCLUDED.last_status,
                               updated_at = EXCLUDED.updated_at,
                               consecutive_failures =
                                   COALESCE(
                                       source_health.consecutive_failures, 0
                                   ) + 1
                        """,
                        self._user_id,
                        self._source_type,
                        now,
                        status,
                    )
        except Exception as exc:
            _logger.warning(
                "PersistentSourceRateLimiter[%s] DB update_last_request failed: %s",
                self._source_type,
                exc,
                exc_info=True,
            )

    async def is_in_cooldown(self) -> tuple[bool, datetime | None]:
        """Read-only check: is this source currently in cooldown?

        Returns
        -------
        tuple[bool, datetime | None]
            ``(True, cooldown_until)`` if in cooldown, ``(False, None)`` otherwise.
        """
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT cooldown_until
                      FROM source_health
                     WHERE (user_id = $1 OR ($1 IS NULL AND user_id IS NULL))
                       AND source_type = $2
                    """,
                    self._user_id,
                    self._source_type,
                )
            if row is None:
                return False, None
            cooldown_until: datetime | None = row["cooldown_until"]
            if cooldown_until is None:
                return False, None
            if cooldown_until > datetime.now(tz=UTC):
                return True, cooldown_until
            return False, None
        except Exception as exc:
            _logger.warning(
                "PersistentSourceRateLimiter[%s] DB is_in_cooldown failed: %s",
                self._source_type,
                exc,
                exc_info=True,
            )
            return False, None

    async def reset(self) -> None:
        """Explicitly clear this source's health (admin / self-heal action).

        Sets ``last_status='ok'``, ``cooldown_until=NULL`` and
        ``consecutive_failures=0`` for this ``(user_id, source_type)`` pair.

        This mirrors the SQL effect of the ``"ok"`` branch of
        :meth:`update_last_request`, but is a deliberate, explicit action
        (operator clears a stuck source, or a self-heal pass clears an
        expired-but-not-reset ``rate_limit`` row) rather than the side effect
        of a successful poll. ``last_request_at`` / ``updated_at`` are stamped
        ``now()`` so the row reflects when the reset happened.

        DB errors are swallowed (logged) so a reset attempt never raises into
        an admin endpoint or a self-heal path.
        """
        now = datetime.now(tz=UTC)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO source_health
                        (user_id, source_type, last_request_at, last_status,
                         cooldown_until, consecutive_failures, updated_at)
                    VALUES ($1, $2, $3, 'ok', NULL, 0, $3)
                    ON CONFLICT (user_id, source_type) DO UPDATE
                       SET last_status = 'ok',
                           cooldown_until = NULL,
                           consecutive_failures = 0,
                           updated_at = EXCLUDED.updated_at
                    """,
                    self._user_id,
                    self._source_type,
                    now,
                )
        except Exception as exc:
            _logger.warning(
                "PersistentSourceRateLimiter[%s] DB reset failed: %s",
                self._source_type,
                exc,
                exc_info=True,
            )

    async def health_snapshot(self) -> dict:
        """Return a self-describing health snapshot for this source.

        Returns
        -------
        dict
            ``{"in_cooldown": bool, "cooldown_until": str|None,
            "last_status": str|None, "last_request_at": str|None,
            "stale": bool}`` where:

            * ``in_cooldown`` — ``cooldown_until`` is set and strictly in the
              future (same rule as :meth:`is_in_cooldown`).
            * ``stale`` — ``last_status='rate_limit'`` while ``cooldown_until``
              is null-or-past, i.e. a *stuck expired* rate-limit state that no
              successful poll ever cleared. Such a row must never be presented
              to a user as a live cooldown.
            * timestamps are ISO-8601 strings (or ``None``).

        On DB error a safe "no data" snapshot is returned (never raises).
        """
        safe: dict = {
            "in_cooldown": False,
            "cooldown_until": None,
            "last_status": None,
            "last_request_at": None,
            "stale": False,
        }
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT cooldown_until, last_status, last_request_at
                      FROM source_health
                     WHERE (user_id = $1 OR ($1 IS NULL AND user_id IS NULL))
                       AND source_type = $2
                    """,
                    self._user_id,
                    self._source_type,
                )
            if row is None:
                return safe
            now = datetime.now(tz=UTC)
            cooldown_until: datetime | None = row["cooldown_until"]
            last_status: str | None = row["last_status"]
            last_request_at: datetime | None = row["last_request_at"]
            in_cooldown = cooldown_until is not None and cooldown_until > now
            cooldown_expired_or_unset = cooldown_until is None or cooldown_until <= now
            stale = last_status == "rate_limit" and cooldown_expired_or_unset
            return {
                "in_cooldown": in_cooldown,
                "cooldown_until": (
                    cooldown_until.isoformat() if cooldown_until is not None else None
                ),
                "last_status": last_status,
                "last_request_at": (
                    last_request_at.isoformat() if last_request_at is not None else None
                ),
                "stale": stale,
            }
        except Exception as exc:
            _logger.warning(
                "PersistentSourceRateLimiter[%s] DB health_snapshot failed: %s",
                self._source_type,
                exc,
                exc_info=True,
            )
            return safe
