"""Direct tests for source-backed search router behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


def _make_source(*, api_key: str | None = None, side_effect=None):
    source = SimpleNamespace(
        source_type="semantic_scholar",
        config=SimpleNamespace(config={"api_key": api_key} if api_key else {}),
        search=AsyncMock(side_effect=side_effect),
    )
    return source


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).
