"""Shared test fixtures for jarvis_common tests.

Infrastructure helpers (live_pg_dsn) are re-exported from
jarvis_common.testing so that the fixture is consistent across services
(--import-mode=importlib + shared tests namespace invariant).
"""

from __future__ import annotations

# live_pg_dsn fixture for this library uses the "jarvis-rd" container prefix.
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")
