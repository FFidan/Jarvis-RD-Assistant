"""Shared test fixtures for paper_ingestion tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies (pypdfium2, tiktoken, qdrant_client, rapidfuzz, docling,
sentence_transformers, apscheduler) are installed on the host venv — no
module stubs are needed.

Infrastructure helpers (FakeRecord, _make_pool_and_conn, live_pg_dsn) are
re-exported from jarvis_common.testing so that ``from tests.conftest import
<symbol>`` resolves identically regardless of which conftest pytest loads
first (--import-mode=importlib + shared tests namespace invariant).
"""

# Pre-import apscheduler.triggers.cron so that per-file stubs in
# test_pulse_scheduler.py cannot replace the real CronTrigger (needed by
# the _validate_cron validator in app.routers.settings).
import apscheduler.triggers.cron  # noqa: F401
import pytest
import pytest_asyncio

# Re-export canonical shared fixtures — keep these names stable; 76 test
# files import them directly via ``from tests.conftest import …``.
from jarvis_common.testing import (  # noqa: F401
    FakeRecord,
    _make_pool_and_conn,
    make_pool_and_conn,
)

# Seed helpers used by the two_users fixture (now canonical in jarvis_common.testing).
from jarvis_common.testing import (  # noqa: F401
    A_CARD_FRONT,
    A_NOTE_TEXT,
    A_PAPER_TITLE,
    A_PROJECT_NAME,
    A_TASK_TITLE,
    TwoUsers,
    _seed_resources,
    _seed_user,
)

# live_pg_dsn fixture for this service uses the "jarvis-rd" container prefix.
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn
from jarvis_common.testing import make_live_pg_session_dsn as _make_live_pg_session_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")

# Cross-user isolation suite: ONE session-scoped container (suffix -xuser) reused
# across all ~53 parametrized cases, with per-test TRUNCATE+reseed in two_users.
# Replaces the per-test throwaway container (docker-daemon saturation under ~53
# serial container spins on a loaded CI runner).
xuser_pg_dsn = _make_live_pg_session_dsn("jarvis-rd")

# Baseline-invariants suite: ONE session container (suffix -baseline) + per-test
# TRUNCATE, replacing the ~23 throwaway containers (one per test).
baseline_pg_dsn = _make_live_pg_session_dsn("jarvis-rd-baseline")

# Contract-layer fixtures: session-scoped Postgres + per-test txn rollback
from jarvis_common.testing import (  # noqa: E402, F401
    _make_contract_conn_fixture,
    _make_contract_pool_fixture,
    _make_contract_two_users_fixture,
)
from jarvis_common.testing import make_contract_pg_dsn as _make_contract_pg_dsn  # noqa: E402
from jarvis_common.testing_contract_apps import (  # noqa: E402
    configure_contract_api_key,
    make_contract_client,
    patch_app_state,
    patch_dependency_overrides,
)

contract_pg_dsn = _make_contract_pg_dsn("jarvis-rd-contract")
_contract_pool = _make_contract_pool_fixture()
contract_conn = _make_contract_conn_fixture()
contract_two_users = _make_contract_two_users_fixture()


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    """Configure the standard contract-test API key for ASGI clients."""
    with configure_contract_api_key(monkeypatch) as key:
        yield key


def _make_client(app, cookie: str):
    """Return the standard contract-test ASGI client for PI contract tests."""
    return make_contract_client(app, cookie)


# Single source of truth for the adversarial-content shape taxonomy. Both the KG
# and Pulse consumer-resilience contract suites parametrize over (subsets of)
# this tuple, and adversarial_llm_payloads keys are exactly these names — so the
# shape list lives in exactly one place.
ADVERSARIAL_SHAPES = (
    "schema_object",
    "think_wrapped",
    "prose_before",
    "truncated",
    "double_encoded",
)


def adversarial_llm_payloads(model, valid_json: str) -> dict[str, str]:
    """Build the five adversarial-content payload shapes for a structured LLM call.

    Each value is a raw string suitable for ``FauxLiteLLMServer.add_response`` —
    it is placed verbatim into ``choices[0].message.content`` and then parsed by
    Instructor against ``model``. Used by the consumer-resilience contract tests
    (Pulse stage-2 degrade path, KG entity-extraction raise path) to prove a
    structured call never silently accepts the JSON schema object as a result.

    Parameters
    ----------
    model:
        The Pydantic response model class whose ``model_json_schema()`` becomes
        the schema-object payload.
    valid_json:
        A valid serialized instance of ``model``, reused to build the
        think-wrapped, prose-prefixed, and double-encoded shapes.
    """
    import json

    payloads = {
        "schema_object": json.dumps(model.model_json_schema()),
        "think_wrapped": "<think>reasoning</think>" + valid_json,
        "prose_before": "Here is the answer: " + valid_json,
        "truncated": valid_json[: max(1, len(valid_json) // 2)],
        "double_encoded": json.dumps(valid_json),
    }
    # Keys must stay in lockstep with the canonical shape taxonomy.
    assert set(payloads) == set(ADVERSARIAL_SHAPES), (
        "adversarial_llm_payloads keys must match ADVERSARIAL_SHAPES; "
        f"payloads={sorted(payloads)} taxonomy={sorted(ADVERSARIAL_SHAPES)}"
    )
    return payloads


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    """Wire the PI app to the per-test contract connection."""
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(app, {"db_pool": shared}),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
        yield app


# Composable contract-app fixture wiring the PI app to a FauxLiteLLMServer
# sidecar. Yields (app, faux_server) for W1+ tests that need to script LLM
# responses without touching real LiteLLM infrastructure. Defined as a
# factory invocation so the canonical implementation lives in jarvis_common.
from jarvis_common.testing import (  # noqa: E402, F401
    _make_pi_contract_app_with_litellm_sidecar,
)

pi_contract_app_with_litellm_sidecar = _make_pi_contract_app_with_litellm_sidecar()


# ---------------------------------------------------------------------------
# Cache-isolation fixture
#
# The root conftest at the repo root defines _clear_secrets_cache, but pytest
# resolves rootdir to services/paper_ingestion/ (due to the local pytest.ini),
# so the repo-root conftest is never loaded for these tests.  This fixture
# replicates the same behaviour locally: clear both the get_secrets_settings
# lru_cache AND the module-level _CACHED_API_KEY in jarvis_common.auth before
# and after every test.  Without it, a test that calls get_secrets_settings()
# with one set of env vars poisons the cache for subsequent tests that rely on
# a different env (e.g. JARVIS_API_KEY="x"*32 leaked into a test expecting
# JARVIS_API_KEY="short").
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_caches():
    """Clear all lru_cache'd settings + the module-level API-key cache."""
    from jarvis_common.auth import refresh_api_key_cache
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()
    refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    refresh_api_key_cache()


@pytest.fixture(autouse=True)
def _default_authenticated_user(request):
    """Cross-user isolation: default every router's strict user-id resolver to user 1.

    Delegates to ``jarvis_common.testing_auth._apply_default_authenticated_user``.
    Tests marked ``@pytest.mark.real_auth`` opt out entirely.
    """
    if request.node.get_closest_marker("real_auth") is not None:
        yield
        return

    import paper_ingestion.routers as routers_pkg
    from jarvis_common.testing_auth import _apply_default_authenticated_user
    from paper_ingestion.main import app

    with _apply_default_authenticated_user(app, routers_pkg):
        yield


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord


# ---------------------------------------------------------------------------
# 4. Pulse subsystem fixture helpers — moved to pulse_helpers.py (D9-07)
#
# Re-exported here so that existing ``from tests.conftest import …`` calls
# in test_pulse_deck.py and test_pulse_profile.py continue to work.
# ---------------------------------------------------------------------------

from tests.pulse_helpers import (  # noqa: F401
    fake_embedding_vector,
    fake_llm_score_response,
    make_pulse_card_row,
    make_pulse_deck_row,
)

# ---------------------------------------------------------------------------
# 5. Database pool fixture for integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
async def test_db_pool(live_pg_dsn):
    """Provide a real asyncpg pool against the live PostgreSQL fixture.

    Applies db/init.sql (the full schema baseline through migration 101)
    then run_migrations(), which is a no-op while db/migrations/ is empty.
    New migrations (0102+) will be picked up automatically when they land.
    """
    import asyncio
    from pathlib import Path

    import asyncpg
    from jarvis_common.db_helpers import init_pg_connection
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).parent.parent.parent.parent / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    migrations_dir = db_dir / "migrations"

    pool = None
    for attempt in range(10):
        try:
            pool = await asyncpg.create_pool(
                live_pg_dsn, min_size=1, max_size=5, init=init_pg_connection
            )
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 9:
                raise
            await asyncio.sleep(0.5)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        await run_migrations(pool, migrations_dir=migrations_dir)
        yield pool
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# 6. Two-user cross-isolation fixture
#
# ONE session-scoped pool over the shared -xuser container; per-test reset via
# TRUNCATE + reseed (committed, so the real SessionMiddleware — which acquires
# its OWN pool connection — sees the session rows under READ COMMITTED).
#
# TwoUsers, A_* constants, _seed_user, _seed_resources are imported from
# jarvis_common.testing.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _xuser_pool(xuser_pg_dsn):
    """Session-scoped asyncpg pool for the cross-user isolation suite.

    Applies db/init.sql + run_migrations() ONCE per session against the single
    ``xuser_pg_dsn`` container. ``init=init_pg_connection`` registers the
    JSON/JSONB codec exactly like the app's lifespan-built pool (response models
    require dict, not str). Per-test reset is done by ``two_users``; this pool is
    created and closed exactly once.
    """
    import asyncio
    from pathlib import Path

    import asyncpg
    from jarvis_common.db_helpers import init_pg_connection
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).parent.parent.parent.parent / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    migrations_dir = db_dir / "migrations"

    pool = None
    for attempt in range(10):
        try:
            pool = await asyncpg.create_pool(
                xuser_pg_dsn, min_size=1, max_size=5, init=init_pg_connection
            )
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 9:
                raise
            await asyncio.sleep(0.5)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
        await run_migrations(pool, migrations_dir=migrations_dir)
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def two_users(_xuser_pool):
    """Two real users, each with a valid session cookie and owned rows.

    Resets the shared session DB to a pristine state (TRUNCATE every public
    table, RESTART IDENTITY CASCADE) then runs the canonical seed — all
    COMMITTED so the real SessionMiddleware can resolve the jarvis_session cookie
    under READ COMMITTED. Truncate-of-all wipes any app writes from the prior
    case (e.g. paper_user_state, user_topic_subscriptions) and prevents the 4
    `global` topic-mutation cases from contaminating later cases. No teardown:
    the next test truncates first; the session pool is owned by ``_xuser_pool``
    and must NOT be closed here.
    """
    async with _xuser_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        names = ", ".join(f'"{r["tablename"]}"' for r in rows)
        if names:
            await conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")

        user_a_id, cookie_a = await _seed_user(conn, "iso-user-a@example.com")
        user_b_id, cookie_b = await _seed_user(conn, "iso-user-b@example.com")
        res_a = await _seed_resources(conn, user_a_id, "a")
        await _seed_resources(conn, user_b_id, "b")

    yield TwoUsers(
        user_a_id=user_a_id,
        user_b_id=user_b_id,
        cookie_a=cookie_a,
        cookie_b=cookie_b,
        paper_id_a=res_a["paper_id"],
        note_id_a=res_a["note_id"],
        card_id_a=res_a["card_id"],
        deck_id_a=res_a["deck_id"],
        project_id_a=res_a["project_id"],
        task_id_a=res_a["task_id"],
        journal_id_a=res_a["journal_id"],
        topic_id_a=res_a["topic_id"],
        pulse_deck_id_a=res_a["pulse_deck_id"],
        pulse_card_id_a=res_a["pulse_card_id"],
        pool=_xuser_pool,
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _baseline_pool(baseline_pg_dsn):
    """Session pool for test_baseline_invariants: db/init.sql applied ONCE."""
    import asyncpg
    from jarvis_common.db_helpers import init_pg_connection
    from tests.migration_helpers import apply_fresh_init

    pool = await asyncpg.create_pool(
        baseline_pg_dsn, min_size=1, max_size=2, init=init_pg_connection
    )
    try:
        await apply_fresh_init(pool)  # schema once; per-test reset is TRUNCATE
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def baseline_conn(_baseline_pool):
    """Pristine connection per test: TRUNCATE all public tables, then yield."""
    async with _baseline_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        names = ", ".join(f'"{r["tablename"]}"' for r in rows)
        if names:
            await conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
        yield conn
