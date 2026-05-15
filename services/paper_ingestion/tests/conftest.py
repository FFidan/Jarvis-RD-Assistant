"""Shared test fixtures for paper_ingestion tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies (fitz, tiktoken, qdrant_client, rapidfuzz, marker,
sentence_transformers, apscheduler) are installed on the host venv — no
module stubs are needed.
"""

import json
import math
import os
import shutil
import subprocess
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

# Pre-import apscheduler.triggers.cron so that per-file stubs in
# test_pulse_scheduler.py cannot replace the real CronTrigger (needed by
# the _validate_cron validator in app.routers.settings).
import apscheduler.triggers.cron  # noqa: F401
import pytest

# ---------------------------------------------------------------------------
# Live PostgreSQL fixture
# ---------------------------------------------------------------------------


def _docker(
    args: list[str],
    *,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a Docker CLI command for opt-in live PostgreSQL tests."""
    return subprocess.run(
        ["docker", *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


@pytest.fixture()
def live_pg_dsn() -> str:
    """Return an asyncpg DSN for a disposable PostgreSQL 16 Docker container.

    The fixture is opt-in because it starts a real container. Set
    ``JARVIS_RUN_LIVE_PG=1`` and run tests marked ``live_pg`` to exercise it.
    """
    if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
        pytest.skip("set JARVIS_RUN_LIVE_PG=1 to run Docker-backed live PostgreSQL tests")
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required for JARVIS_RUN_LIVE_PG=1 live PostgreSQL tests")

    container = f"jarvis-rd-live-pg-{uuid.uuid4().hex[:12]}"
    password = f"jarvis-test-{uuid.uuid4().hex}"
    image = os.environ.get("JARVIS_LIVE_PG_IMAGE", "postgres:16.8")
    _docker(
        [
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_DB=jarvis",
            "-e",
            "POSTGRES_USER=jarvis",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            "127.0.0.1::5432",
            image,
        ]
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = _docker(
                ["exec", container, "pg_isready", "-U", "jarvis", "-d", "jarvis"],
                check=False,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            logs = _docker(["logs", container], check=False, timeout=10)
            pytest.fail(f"PostgreSQL container did not become ready:\n{logs.stdout}{logs.stderr}")

        port_result = _docker(["port", container, "5432/tcp"])
        host_port = port_result.stdout.strip().rsplit(":", maxsplit=1)[-1]
        yield f"postgresql://jarvis:{password}@127.0.0.1:{host_port}/jarvis"
    finally:
        _docker(["rm", "-f", container], check=False, timeout=10)


# ---------------------------------------------------------------------------
# FakeRecord + shared fixtures
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Unified asyncpg.Record substitute: dict[], .attr, .keys(), .get(), .values()."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


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
    try:
        yield
    finally:
        for module, name, original in saved:
            setattr(module, name, original)


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord


# ---------------------------------------------------------------------------
# 4. Pulse subsystem fixture helpers (Stream F0 — consumed by Streams A/B/C/D)
# ---------------------------------------------------------------------------


def make_pulse_deck_row(
    deck_date: str = "2024-01-15",
    card_count: int = 10,
    stats: dict | None = None,
) -> FakeRecord:
    """Return a FakeRecord matching the pulse_decks schema.

    Parameters
    ----------
    deck_date:
        ISO date string (YYYY-MM-DD) or a datetime.date for ``deck_date``.
    card_count:
        Number of cards in the deck.
    stats:
        Optional JSONB stats dict (candidate_count, llm_calls, duration_s, etc.).
    """
    return FakeRecord(
        {
            "id": 1,
            "deck_date": deck_date,
            "card_count": card_count,
            "generated_at": "2024-01-15T04:00:00+00:00",
            "stats": stats if stats is not None else {},
        }
    )


def make_pulse_card_row(
    deck_id: int = 1,
    paper_id: int = 42,
    rank: int = 1,
    score: float = 0.85,
    reasoning: str = "Highly relevant to your active topics.",
    signals: dict | None = None,
) -> FakeRecord:
    """Return a FakeRecord matching the pulse_cards schema.

    Parameters
    ----------
    deck_id:
        FK to pulse_decks.id.
    paper_id:
        FK to papers.id.
    rank:
        1-based rank within the deck (lower = more relevant).
    score:
        Composite score in [0, 1].
    reasoning:
        One-sentence LLM explanation for inclusion.
    signals:
        Per-signal score breakdown, e.g. ``{"embedding": 0.82, "topic": 0.74}``.
    """
    return FakeRecord(
        {
            "id": rank,
            "deck_id": deck_id,
            "paper_id": paper_id,
            "rank": rank,
            "score": score,
            "llm_relevance": 8,
            "llm_novelty": 6,
            "reasoning": reasoning,
            "signals": signals if signals is not None else {"embedding": 0.82, "topic": 0.74},
            "created_at": "2024-01-15T04:00:01+00:00",
        }
    )


def make_pdf_resolution_row(
    doi: str | None = "10.1234/example",
    arxiv_id: str | None = None,
    resolved_url: str | None = "https://arxiv.org/pdf/2401.00001",
    resolver_name: str = "arxiv",
) -> FakeRecord:
    """Return a FakeRecord matching the pdf_resolutions schema.

    Parameters
    ----------
    doi:
        Canonical DOI or None for arXiv-only papers.
    arxiv_id:
        arXiv identifier or None for DOI-only papers.
    resolved_url:
        PDF URL if resolution succeeded; None if all resolvers failed
        (cached failure marker).
    resolver_name:
        Which resolver produced the result (``'arxiv'``, ``'unpaywall'``,
        ``'core'``, or ``'failed'``).
    """
    return FakeRecord(
        {
            "id": 1,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "resolved_url": resolved_url,
            "resolver_name": resolver_name,
            "resolved_at": "2024-01-15T04:05:00+00:00",
        }
    )


def fake_embedding_vector(dim: int = 1024) -> list[float]:
    """Return a deterministic unit-ish embedding vector of length ``dim``.

    Values cycle through a simple pattern so tests are reproducible without
    importing numpy.  For callers that need a numpy array, wrap with
    ``np.array(fake_embedding_vector())``.
    """
    # Deterministic, non-trivial values: sin(i / dim * pi) normalised
    raw = [math.sin(i / max(dim, 1) * math.pi) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def fake_llm_score_response(
    relevance: int = 7,
    novelty: int = 5,
    reasoning: str = "This paper directly addresses your active research topics.",
) -> str:
    """Return the JSON string a mocked LLM would produce for Pulse Stage 2 scoring.

    Parameters
    ----------
    relevance:
        Integer 1-10 relevance score.
    novelty:
        Integer 1-10 novelty score.
    reasoning:
        One-sentence explanation string.
    """
    return json.dumps(
        {
            "relevance": relevance,
            "novelty": novelty,
            "reasoning": reasoning,
        }
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

    Provisioning has to work around a *pre-existing* repo schema-drift
    defect (the canonical fresh-init test ``test_migrations_live`` is
    itself red on master for the same reason; ``test_db_pool``'s tolerant
    ``;``-splitter merely swallows it into a half-built schema). Concretely:

      1. ``init.sql`` references ``users(id)`` (``user_topic_subscriptions``
         FK) but never creates ``users``/``sessions`` — those live only in
         migration 069. So apply the idempotent 069 auth DDL first.
      2. ``init.sql`` is *mostly* the post-migration steady state but
         genuinely lags a handful of migrations whose per-user columns the
         endpoints query (070 ``cards/decks.user_id``; 072 ``papers``
         ``user_id``->``discovered_by`` rename + ``user_library``).
      3. Re-running the full migration set on top of init.sql conflicts on
         objects init.sql already has (e.g. 063's
         ``uq_paper_recommendations`` unique index → "relation already
         exists", which 063's ``EXCEPTION WHEN duplicate_object`` does NOT
         catch).

    Strategy that yields a correct, complete schema:
      * apply 069 auth DDL + ``init.sql`` whole;
      * pre-mark *every* migration version applied, then run the real
        runner — its ``_repair_false_applied_migrations`` probe replays
        only the genuinely-missing probe-covered ones (33/49/50/63/77…)
        and skips everything init.sql already has (no conflicts);
      * finally, explicitly re-apply the *idempotent* migrations init.sql
        is known to lag (070 then 072) so the per-user scoping columns the
        routers SELECT actually exist. Both are guarded
        (``ADD COLUMN IF NOT EXISTS`` / ``DO $$ … EXCEPTION``) so a second
        application is a safe no-op.

    The container is torn down by ``live_pg_dsn`` after the test, so no
    row-level cleanup is required — the DB is disposable per test.
    """
    from pathlib import Path

    import asyncpg
    from jarvis_common.db_helpers import init_pg_connection
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).parent.parent.parent.parent / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    migrations_dir = db_dir / "migrations"
    auth_ddl = (migrations_dir / "069_auth.sql").read_text(encoding="utf-8")
    all_versions = sorted(
        int(p.name.split("_")[0])
        for p in migrations_dir.glob("*.sql")
        if p.name.split("_")[0].isdigit()
    )
    # Migrations init.sql lags whose columns the endpoints query. Applied
    # in version order after the runner; all are fully idempotent
    # (ADD COLUMN IF NOT EXISTS / guarded DO $$ blocks) so re-applying what
    # the runner already did is a safe no-op:
    #   034 — pulse_cards.reasoning_verified/_confidence (pulse/today query)
    #   070 — cards/decks.user_id (cards/decks scoping queries)
    #   072 — papers user_id->discovered_by rename + user_library
    lagged_migrations = (
        migrations_dir / "034_pulse_reasoning_verification.sql",
        migrations_dir / "070_multi_tenant_user_id_columns.sql",
        migrations_dir / "072_canonical_corpus.sql",
    )

    # init=init_pg_connection registers the JSON/JSONB codec exactly like the
    # app's lifespan-built pool, so JSONB columns deserialize to dicts (the
    # response models require dict, not str).
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=5, init=init_pg_connection)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute(auth_ddl)  # users + sessions (IF NOT EXISTS)
            await conn.execute(init_sql)  # everything else, FK now resolvable
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.executemany(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                [(v,) for v in all_versions],
            )
        await run_migrations(pool, migrations_dir=migrations_dir)
        async with pool.acquire() as conn:
            for mig in lagged_migrations:
                await conn.execute(mig.read_text(encoding="utf-8"))

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
