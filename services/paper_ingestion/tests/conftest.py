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

# Re-export canonical shared fixtures — keep these names stable; 76 test
# files import them directly via ``from tests.conftest import …``.
from jarvis_common.testing import (  # noqa: F401
    FakeRecord,
    _make_pool_and_conn,
    make_pool_and_conn,
)

# live_pg_dsn fixture for this service uses the "jarvis-rd" container prefix.
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")


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
    """Provide a real asyncpg pool connected to the live PostgreSQL fixture.

    Runs migrations and creates all schema tables before each test,
    then clears them afterward.
    """
    from pathlib import Path

    import asyncpg

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=5)
    assert pool is not None

    # Apply initial schema
    # db/init.sql is at repo root, not under services/
    db_dir = Path(__file__).parent.parent.parent.parent / "db"
    init_sql = (db_dir / "init.sql").read_text()

    async with pool.acquire() as conn:
        # Split init.sql by semicolons and execute statement by statement
        # This prevents transaction abort if one statement fails
        if init_sql.strip():
            statements = [s.strip() for s in init_sql.split(";") if s.strip()]
            for stmt in statements:
                try:
                    await conn.execute(stmt)
                except Exception:
                    # Some statements may fail if objects already exist
                    pass

        # Apply migrations to set up schema
        migrations_dir = db_dir / "migrations"
        migration_files = sorted(migrations_dir.glob("*.sql"))

        for mig_file in migration_files:
            sql = mig_file.read_text()
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                try:
                    await conn.execute(stmt)
                except Exception:
                    # Migrations may have already been applied; skip on error
                    pass

    yield pool

    # Clean up: drop all tables (reverse order to respect FKs)
    tables_to_drop = [
        "recommendations",
        "recommendation_feedback",
        "pulse_ratings",
        "pulse_cards",
        "pulse_decks",
        "pdf_resolutions",
        "paper_embedding_chunks",
        "entity_mentions",
        "entity_types",
        "entities",
        "citations",
        "paper_user_state",
        "papers",
        "learning_jobs",
        "learning_job_batches",
        "user_config",
        "projects",
        "topics",
        "telegram_conversations",
    ]

    async with pool.acquire() as conn:
        for table in tables_to_drop:
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    await pool.close()


# ---------------------------------------------------------------------------
# 6. Two-user cross-isolation fixture (WS-NEGATIVE-TESTS)
# ---------------------------------------------------------------------------


class TwoUsers:
    """Handle exposing two real DB users plus their seeded, owned resources.

    Every ``*_a`` attribute is owned by ``user_a_id``; the negative test acts
    as user B (``cookie_b``) and asserts it can neither read nor mutate any
    of A's rows. ``cookie_*`` are ready-to-use ``jarvis_session`` cookie
    values (the session row's UUID id).
    """

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)

    user_a_id: int
    user_b_id: int
    cookie_a: str
    cookie_b: str
    paper_id_a: int
    note_id_a: int
    card_id_a: int
    deck_id_a: int
    project_id_a: int
    task_id_a: int
    journal_id_a: int
    topic_id_a: int
    pulse_deck_id_a: int
    pulse_card_id_a: int
    pool: object  # asyncpg.Pool — live schema, used for app wiring + re-checks


# Marker strings the test asserts are NEVER visible to user B.
A_PAPER_TITLE = "ZZZ-ISOLATION-A-PAPER Quantum Entanglement of Owls"
A_NOTE_TEXT = "ZZZ-ISOLATION-A-NOTE private annotation alpha"
A_PROJECT_NAME = "ZZZ-ISOLATION-A-PROJECT secret roadmap"
A_TASK_TITLE = "ZZZ-ISOLATION-A-TASK confidential milestone"
A_CARD_FRONT = "ZZZ-ISOLATION-A-CARD front side alpha"


async def _seed_user(conn, email: str) -> tuple[int, str]:
    """Insert one active user + one valid session; return (user_id, cookie)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        email,
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day')
           RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


async def _seed_resources(conn, user_id: int, tag: str) -> dict:
    """Seed one owned row per DB-backed table the endpoints read."""
    # Sprint-B canonical-corpus model (migration 072): papers are global;
    # ownership = `papers.discovered_by` OR `user_library` membership
    # (see jarvis_common.db_helpers.assert_paper_ownership). So the paper is
    # owned by this user via discovered_by, and we also record explicit
    # library membership for realism.
    paper_id = await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', $2, ARRAY['A. Author'], 'https://example.test/a', $3)
           RETURNING id""",
        f"iso-ext-{tag}",
        A_PAPER_TITLE if tag == "a" else f"paper-{tag}",
        user_id,
    )
    await conn.execute(
        """INSERT INTO user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        paper_id,
    )
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, starred)
           VALUES ($1, $2, 'to_read', TRUE)""",
        paper_id,
        user_id,
    )
    note_id = await conn.fetchval(
        """INSERT INTO paper_notes (paper_id, user_note, user_id)
           VALUES ($1, $2, $3) RETURNING id""",
        paper_id,
        A_NOTE_TEXT if tag == "a" else f"note-{tag}",
        user_id,
    )
    # decks/cards carry a user_id column (migration 070). Ownership is
    # router-enforced by that column: decks.py:53 `WHERE d.user_id = $1`
    # and cards.py:119 `SELECT * FROM cards WHERE id = $1 AND user_id = $2`.
    # Seed the owning user_id so cross-user access is genuinely denied
    # (NULL would let both users read the rows -> false-green).
    deck_id = await conn.fetchval(
        "INSERT INTO decks (name, user_id) VALUES ($1, $2) RETURNING id",
        f"deck-{tag}",
        user_id,
    )
    card_id = await conn.fetchval(
        """INSERT INTO cards (deck_id, paper_id, card_type, front, back, user_id)
           VALUES ($1, $2, 'concept', $3, 'back', $4) RETURNING id""",
        deck_id,
        paper_id,
        A_CARD_FRONT if tag == "a" else f"card-{tag}",
        user_id,
    )
    project_id = await conn.fetchval(
        """INSERT INTO projects (name, user_id) VALUES ($1, $2) RETURNING id""",
        A_PROJECT_NAME if tag == "a" else f"project-{tag}",
        user_id,
    )
    task_id = await conn.fetchval(
        """INSERT INTO tasks (project_id, title, user_id)
           VALUES ($1, $2, $3) RETURNING id""",
        project_id,
        A_TASK_TITLE if tag == "a" else f"task-{tag}",
        user_id,
    )
    journal_id = await conn.fetchval(
        """INSERT INTO journal_entries (user_id, date, prompts)
           VALUES ($1, CURRENT_DATE, '{"win": "secret"}'::jsonb)
           RETURNING id""",
        user_id,
    )
    topic_id = await conn.fetchval(
        """INSERT INTO topics (name, query_terms) VALUES ($1, ARRAY['q'])
           RETURNING id""",
        f"topic-{tag}",
    )
    await conn.execute(
        """INSERT INTO user_topic_subscriptions (user_id, topic_id)
           VALUES ($1, $2)""",
        user_id,
        topic_id,
    )
    await conn.execute(
        """INSERT INTO paper_recommendations (paper_id, score, user_id)
           VALUES ($1, 0.9, $2)""",
        paper_id,
        user_id,
    )
    pulse_deck_id = await conn.fetchval(
        """INSERT INTO pulse_decks (deck_date, card_count, user_id)
           VALUES (CURRENT_DATE, 1, $1) RETURNING id""",
        user_id,
    )
    pulse_card_id = await conn.fetchval(
        """INSERT INTO pulse_cards (deck_id, paper_id, rank, score, user_id)
           VALUES ($1, $2, 1, 0.9, $3) RETURNING id""",
        pulse_deck_id,
        paper_id,
        user_id,
    )
    return {
        "paper_id": paper_id,
        "note_id": note_id,
        "card_id": card_id,
        "deck_id": deck_id,
        "project_id": project_id,
        "task_id": task_id,
        "journal_id": journal_id,
        "topic_id": topic_id,
        "pulse_deck_id": pulse_deck_id,
        "pulse_card_id": pulse_card_id,
    }


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
            user_a_id, cookie_a = await _seed_user(conn, "iso-user-a@example.test")
            user_b_id, cookie_b = await _seed_user(conn, "iso-user-b@example.test")
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
