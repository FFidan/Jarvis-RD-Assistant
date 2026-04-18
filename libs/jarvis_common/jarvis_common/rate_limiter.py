"""Token-bucket rate limiter for source plugins.

Provides :class:`SourceRateLimiter` — a simple asyncio-compatible token-bucket
that caps the request rate for external API sources (arXiv, Semantic Scholar,
OpenAlex, PubMed, …).
"""

import asyncio


class SourceRateLimiter:
    """Token-bucket rate limiter for source plugins.

    Parameters
    ----------
    rate_per_second:
        Sustained request rate (tokens refilled per second).
    burst:
        Maximum token bucket capacity (allows short bursts above the sustained
        rate). Defaults to 1 (no bursting).
    """

    def __init__(self, rate_per_second: float, burst: int = 1) -> None:
        self.rate = rate_per_second
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire one token, sleeping if the bucket is empty."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0
