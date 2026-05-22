"""Shared test fixtures for paper_ingestion tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies (fitz, tiktoken, qdrant_client, rapidfuzz, marker,
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

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")

# Contract-layer fixtures (Wave 4): session-scoped Postgres + per-test txn rollback
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
    """WS-CROSS-USER: default every router's strict user-id resolver to user 1.

    The strict resolvers (``current_user_id_strict`` /
    ``current_user_id_strict_with_owner_override``) hard-401 sessionless
    callers. The vast majority of unit tests call route bodies directly with
    a stub request and only assert SQL/response shape — they predate auth and
    have no session. This fixture patches each router module's resolver symbol
    to return a concrete test user so those tests exercise the real isolation
    SQL path.

    Auth/IDOR tests that need a specific user (or 401) re-patch the same
    module attribute inside their own ``with patch(...)`` / ``monkeypatch``
    scope, which takes precedence for the duration of the test body.

    Tests marked ``@pytest.mark.real_auth`` opt OUT of this stub entirely:
    they exercise the genuine ``SessionMiddleware`` -> ``request.state.user_id``
    -> strict-resolver path against a real ``jarvis_session`` cookie. The
    opt-out is inert (no behaviour change) when the marker is absent.
    """
    if request.node.get_closest_marker("real_auth") is not None:
        yield
        return

    import importlib
    import pkgutil
    from unittest.mock import AsyncMock

    import paper_ingestion.routers as routers_pkg
    from jarvis_common.auth import current_user_id_strict_with_owner_override
    from paper_ingestion.main import app

    resolver_names = (
        "current_user_id_strict",
        "current_user_id_strict_with_owner_override",
    )
    saved: list[tuple[object, str, object]] = []
    for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"paper_ingestion.routers.{mod_info.name}")
        for name in resolver_names:
            if hasattr(module, name):
                saved.append((module, name, getattr(module, name)))
                setattr(module, name, AsyncMock(return_value=1))

    # CC-03: handlers converted to ``Depends(get_current_user_id)`` resolve
    # identity through FastAPI's dependency graph, which the symbol monkeypatch
    # above cannot intercept (``Depends`` captured the function object at import
    # time). ``get_current_user_id`` is a thin wrapper whose body is
    # ``Depends(current_user_id_strict_with_owner_override)``, so overriding the
    # *inner* resolver lets FastAPI's recursive override resolution default
    # every converted route — and the pre-existing declarative
    # ``Depends(current_user_id_strict_with_owner_override)`` routes
    # (e.g. pulse ``explain_card``) — to user 1.
    #
    # Overriding the inner resolver (not the wrapper) is deliberate: a test that
    # needs a specific attacker/owner id re-assigns this SAME dict key inside
    # its own scope, and the last assignment wins — exactly the precedence the
    # old per-router symbol monkeypatch provided. Overriding the wrapper instead
    # would short-circuit FastAPI before it descends to the inner resolver, so
    # such per-test re-overrides would be silently ignored.
    override_added = current_user_id_strict_with_owner_override not in app.dependency_overrides
    if override_added:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 1
    try:
        yield
    finally:
        for module, name, original in saved:
            setattr(module, name, original)
        if override_added:
            app.dependency_overrides.pop(current_user_id_strict_with_owner_override, None)


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
    make_pdf_resolution_row,
    make_pulse_card_row,
    make_pulse_deck_row,
)

# ---------------------------------------------------------------------------
# 5. Database pool fixture for integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
async def test_db_pool(live_pg_dsn):
    """Provide a real asyncpg pool against the live PostgreSQL fixture.

    Applies db/init.sql (the full single-baseline schema post-Wave-1)
    + run_migrations() (no-op against the empty db/migrations/ today;
    future-proofs for 0089+).
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
# 6. Two-user cross-isolation fixture (WS-NEGATIVE-TESTS)
#
# TwoUsers, A_* constants, _seed_user, _seed_resources are imported from
# jarvis_common.testing (D5 — moved in Wave 4).
# ---------------------------------------------------------------------------


@pytest.fixture()
async def two_users(live_pg_dsn):
    """Two real users, each with a valid session cookie and owned rows.

    Builds its OWN asyncpg pool against the disposable ``live_pg_dsn``
    container and provisions a complete schema.

    Wave-1 squash (c6145af3): ``db/init.sql`` now embodies all 88 migrations
    including the auth tables (``users``, ``sessions``, ``magic_link_tokens``).
    Apply ``init.sql`` alone; ``run_migrations`` over the now-empty
    ``db/migrations/`` is a clean no-op that just confirms no 0089+ tail
    exists yet.

    The container is torn down by ``live_pg_dsn`` after the test, so no
    row-level cleanup is required — the DB is disposable per test.
    """
    import asyncio
    from pathlib import Path

    import asyncpg
    from jarvis_common.db_helpers import init_pg_connection
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).parent.parent.parent.parent / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    migrations_dir = db_dir / "migrations"

    # init=init_pg_connection registers the JSON/JSONB codec exactly like the
    # app's lifespan-built pool, so JSONB columns deserialize to dicts (the
    # response models require dict, not str).
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
            await conn.execute(init_sql)  # embodies all schema incl. users/sessions
        await run_migrations(pool, migrations_dir=migrations_dir)

        async with pool.acquire() as conn:
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
            pool=pool,
        )
    finally:
        await pool.close()
