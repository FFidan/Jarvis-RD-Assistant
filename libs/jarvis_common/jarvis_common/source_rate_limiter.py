"""Token-bucket rate limiter for source plugins.

Provides :class:`SourceRateLimiter` — a simple asyncio-compatible token-bucket
that caps the request rate for external API sources (arXiv, Semantic Scholar,
OpenAlex, PubMed, …).
"""

import asyncio
import time


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
                self.tokens = 0.0
            else:
                self.tokens -= 1.0
