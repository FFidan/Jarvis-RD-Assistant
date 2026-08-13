"""Jobs REST endpoints for learning_engine service.

Thin shim over :func:`jarvis_common.jobs_router.build_jobs_router` — see that
module for endpoint contracts and shared invariants (LE-002 ownership
coercion, mutable-default fix, ``noop.test`` toggle).

``card.generate_batch`` is intentionally excluded from the public allowlist;
that batch operation is dispatched through ``POST /api/generate/batch`` with
its own validation.
"""

from __future__ import annotations

from typing import Literal

from jarvis_common.jobs_router import build_jobs_router
from pydantic import BaseModel

from learning_engine.deps import get_db_pool, limiter

# ---------------------------------------------------------------------------
# Allowlist of job kinds clients may create via POST /api/jobs.
# ---------------------------------------------------------------------------
LE_PUBLIC_JOB_KINDS: frozenset[str] = frozenset({"card.generate"})


class _CardGeneratePayload(BaseModel):
    """Strict payload schema for card.generate jobs.

    Requiring paper_id enables the paper-ownership extractor to gate
    cross-user paper access at enqueue time (RD-DA-001).
    """

    kind: Literal["card.generate"]
    paper_id: int
    deck_id: int
    max_cards: int = 5


def _card_generate_paper_extractor(payload: dict) -> int | None:
    v = payload.get("paper_id")
    return v if isinstance(v, int) else None


router = build_jobs_router(
    public_kinds=LE_PUBLIC_JOB_KINDS,
    get_db_pool=get_db_pool,
    limiter=limiter,
    payload_schemas={"card.generate": _CardGeneratePayload},
    paper_ownership_extractor=_card_generate_paper_extractor,
)

__all__ = [
    "LE_PUBLIC_JOB_KINDS",
    "router",
]
