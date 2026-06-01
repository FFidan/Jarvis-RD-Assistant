"""Backwards-compatible facade. All implementations live in submodules.

This module preserves the ``from jarvis_common.testing import X`` import path
used by ~70 test files across 4 services. The actual implementations are
decomposed per the 2026-05-24 polish-wave decomposition:

  - clusters 1-5  -> testing_db.py            (NEW)
  - cluster 6     -> testing_auth.py          (NEW)
  - clusters 7-8  -> testing_telegram.py      (NEW)
  - cluster 9     -> testing_search.py        (NEW)
  - cluster 10    -> testing_contract_apps.py (EXTENDED — pre-existing 121 LOC file)

``testing_sidecars/`` is UNRELATED to this decomposition and remains untouched.

Public API
----------
FakeRecord              asyncpg.Record dict-shim (attr + .get access)
make_pool_and_conn      canonical mock (pool, conn) factory with optional kwargs
_make_pool_and_conn     module-level alias preserved for existing importers
make_request            minimal request mock for handler tests
make_live_pg_dsn        factory that returns a ``live_pg_dsn`` pytest fixture
make_contract_pg_dsn    factory that returns a session-scoped ``contract_pg_dsn`` fixture
SharedAcquireCM         async CM yielding a shared asyncpg connection under a reentrant lock
SharedConnPool          asyncpg.Pool-shaped object backed by a single shared connection
TwoUsers                seed handle for cross-user IDOR contract tests
RoleMiddleware          ASGI middleware shim that injects request.state.user_role
FakeAcquireCM           async CM returned by pool.acquire() in telegram tests
FakeTxnCM               async CM returned by conn.transaction() in telegram tests
make_telegram_update    build a minimal PTB Update-like MagicMock
make_bot_config         build a minimal BotConfig for telegram_bot tests
ScriptedReranker        in-process DI seam replacing CrossEncoder
"""

from __future__ import annotations

__all__ = [
    # cluster 1-5 (testing_db)
    "FakeRecord",
    "make_pool_and_conn",
    "_make_pool_and_conn",
    "make_request",
    "make_live_pg_dsn",
    "make_live_pg_session_dsn",
    "make_contract_pg_dsn",
    "_make_contract_pool_fixture",
    "_make_contract_conn_fixture",
    "SharedAcquireCM",
    "SharedConnPool",
    "TwoUsers",
    "_make_contract_two_users_fixture",
    "A_PAPER_TITLE",
    "A_NOTE_TEXT",
    "A_PROJECT_NAME",
    "A_TASK_TITLE",
    "A_CARD_FRONT",
    # cluster 6 (testing_auth)
    "RoleMiddleware",
    # clusters 7-8 (testing_telegram)
    "FakeAcquireCM",
    "FakeTxnCM",
    "make_telegram_update",
    "make_bot_config",
    # cluster 9 (testing_search)
    "ScriptedReranker",
    # cluster 10 (testing_contract_apps)
    "_make_pi_contract_app_with_litellm_sidecar",
    "_make_le_contract_app_with_litellm_sidecar",
]

from jarvis_common.testing_auth import RoleMiddleware  # noqa: F401
from jarvis_common.testing_contract_apps import (  # noqa: F401
    _make_le_contract_app_with_litellm_sidecar,
    _make_pi_contract_app_with_litellm_sidecar,
)
from jarvis_common.testing_db import (  # noqa: F401
    A_CARD_FRONT,
    A_NOTE_TEXT,
    A_PAPER_TITLE,
    A_PROJECT_NAME,
    A_TASK_TITLE,
    FakeRecord,
    SharedAcquireCM,
    SharedConnPool,
    TwoUsers,
    _make_contract_conn_fixture,
    _make_contract_pool_fixture,
    _make_contract_two_users_fixture,
    _make_pool_and_conn,
    make_contract_pg_dsn,
    make_live_pg_dsn,
    make_live_pg_session_dsn,
    make_pool_and_conn,
    make_request,
)
from jarvis_common.testing_db import (
    _seed_resources as _seed_resources,
)
from jarvis_common.testing_db import (
    _seed_user as _seed_user,
)
from jarvis_common.testing_search import ScriptedReranker  # noqa: F401
from jarvis_common.testing_telegram import (  # noqa: F401
    FakeAcquireCM,
    FakeTxnCM,
    make_bot_config,
    make_telegram_update,
)
