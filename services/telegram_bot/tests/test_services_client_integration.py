"""Cross-service integration smoke: services_client ↔ real LE / PI ASGI apps.

Drives the bot's ``services_client`` functions against real FastAPI apps
(Learning Engine + Paper Ingestion) wired to a live PostgreSQL database.  The
goal is to catch contract DRIFT between the bot client (pinned to URL/param/
response-key contracts) and the actual service endpoints — the per-side unit
and contract tests cannot detect mismatches at the seam.

Strategy
--------
Option (b) from the task description: one ASGI client per service, each
pointed at its own app with a matching ``base_url``.  The ``services_client``
functions derive their target URLs from ``config.learning_engine_url`` and
``config.paper_ingestion_url``, so both are set to ``"http://test"`` and the
corresponding ``httpx.AsyncClient(transport=ASGITransport(app=…), base_url=
"http://test")`` clients route in-process.

Auth wiring
-----------
Both apps require ``X-API-Key: <key>`` (global ``Depends(verify_api_key)``).
The owner-override path additionally requires:
  1. ``X-API-Key`` matching the module-level ``_CACHED_API_KEY``
  2. ``X-Owner-User-Id`` carrying the seeded user's integer id
  3. The client IP is on the allowlist — patched via
     ``monkeypatch.setattr("jarvis_common.auth._ip_in_allowlist", lambda _ip: True)``
     because ``ASGITransport`` presents as ``testclient`` rather than a real
     routable IP.

``configure_contract_api_key`` sets ``JARVIS_API_KEY`` in the environment and
refreshes the auth cache so ``verify_api_key`` + ``_CACHED_API_KEY`` both see
the same key that the client sends.

Endpoints covered
-----------------
- ``fetch_tasks``              GET  /api/tasks               (LE) — project_name key
- ``fetch_upcoming_milestones``GET  /api/milestones/upcoming (LE) — name/deadline/project_name
- ``complete_task``            PUT  /api/tasks/{id}          (LE) — done + daily_log bump
- ``check_authors``   POST /api/authors/check  (PI) — matches/new_papers/authors_checked

Limitation
----------
``check_authors`` calls ``current_user_id_strict_with_owner_override(request)``
*inline* (not via ``Depends``), so the dependency-override dict is bypassed.
The IP-allowlist patch + matching API-key in the ``X-API-Key`` header + a real
DB user row in the pool handle auth end-to-end — no extra override needed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import httpx
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.migrations import run_migrations
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn
from jarvis_common.testing_contract_apps import (
    configure_contract_api_key,
    patch_app_state,
    patch_dependency_overrides,
)
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.services_client import (
    check_authors,
    complete_task,
    fetch_tasks,
    fetch_upcoming_milestones,
)

# ---------------------------------------------------------------------------
# Live-PG DSN fixture (one throwaway container for this whole suite)
# ---------------------------------------------------------------------------

_live_pg_dsn = _make_live_pg_dsn("jarvis-tg-int")

# Re-expose under the canonical name so pytest resolves the function-scoped
# fixture; the container spins up once per test function that requests it.
live_pg_dsn = _live_pg_dsn


# ---------------------------------------------------------------------------
# Session-scoped pool — schema applied once; all integration tests share it
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _int_pool(live_pg_dsn: str):
    """Real asyncpg pool against the live-PG container with schema applied."""
    db_dir = Path(__file__).resolve().parents[3] / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    migrations_dir = db_dir / "migrations"

    pool: asyncpg.Pool | None = None
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
# Seed helper — one admin user with a project, task, milestone, and
# tracked author + a recent paper so check_authors can return a non-trivial
# response.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _seed(_int_pool):
    """Seed the live DB and return a dict of ids/values for the tests."""
    async with _int_pool.acquire() as conn:
        user_id: int = await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ('int-test@example.com', 'user') RETURNING id"
        )
        project_id: int = await conn.fetchval(
            "INSERT INTO projects (name, user_id) VALUES ('Integration Project', $1) RETURNING id",
            user_id,
        )
        task_id: int = await conn.fetchval(
            "INSERT INTO tasks (project_id, title, status, user_id)"
            " VALUES ($1, 'Integration Task', 'todo', $2) RETURNING id",
            project_id,
            user_id,
        )
        # Milestone due within 3 days from now
        milestone_deadline = datetime.now(UTC) + timedelta(days=2)
        milestone_id: int = await conn.fetchval(
            "INSERT INTO milestones (project_id, name, deadline, user_id)"
            " VALUES ($1, 'Integration Milestone', $2, $3) RETURNING id",
            project_id,
            milestone_deadline,
            user_id,
        )
        # A tracked author for check_authors
        await conn.execute(
            "INSERT INTO tracked_authors (author_name, source, enabled, user_id)"
            " VALUES ('Integration Author', 'manual', TRUE, $1)",
            user_id,
        )
        # A paper published in the last hour with matching author (triggers a match)
        paper_id: int = await conn.fetchval(
            "INSERT INTO papers"
            " (external_id, source_type, title, authors, url, discovered_by)"
            " VALUES ('int-ext-1', 'arxiv', 'Integration Paper',"
            "         ARRAY['Integration Author'], 'https://example.test/int', $1)"
            " RETURNING id",
            user_id,
        )
        # created_at defaults to NOW() — within the 24h window the check uses
    yield {
        "user_id": user_id,
        "project_id": project_id,
        "task_id": task_id,
        "milestone_id": milestone_id,
        "paper_id": paper_id,
        "milestone_deadline": milestone_deadline,
    }


# ---------------------------------------------------------------------------
# App wiring helpers — inject the live pool into LE and PI apps
# ---------------------------------------------------------------------------


def _le_app_wired(pool: asyncpg.Pool):
    """Return a context manager that patches the LE app state with *pool*."""
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager
    from learning_engine.deps import limiter as le_limiter
    from learning_engine.main import app as le_app

    now = datetime.now(UTC)
    mock_fsrs = MagicMock()
    mock_fsrs.create_new_card.return_value = ({}, now)
    mock_fsrs.schedule_review.return_value = ({}, {}, now + timedelta(days=1))
    mock_exporter = MagicMock()
    mock_exporter.export_deck.return_value = bytes.fromhex("504b0506") + b"\x00" * 18

    # Build a combined context manager that:
    #   1. Disables the rate-limiter (avoids 429 noise)
    #   2. Patches app.state with the live pool + mock collaborators
    #   3. Patches dependency overrides so Depends(get_db_pool) returns *pool*
    from contextlib import ExitStack

    stack = ExitStack()
    original_enabled = le_limiter.enabled
    le_limiter.enabled = False

    stack.callback(setattr, le_limiter, "enabled", original_enabled)
    stack.enter_context(
        patch_app_state(
            le_app,
            {
                "db_pool": pool,
                "http_client": AsyncMock(),
                "fsrs_manager": mock_fsrs,
                "anki_exporter": mock_exporter,
                "card_generator": AsyncMock(),
            },
        )
    )
    stack.enter_context(
        patch_dependency_overrides(
            le_app,
            set_overrides={
                get_db_pool: lambda: pool,
                get_fsrs_manager: lambda: mock_fsrs,
                get_anki_exporter: lambda: mock_exporter,
            },
        )
    )
    return stack


def _pi_app_wired(pool: asyncpg.Pool):
    """Return a context manager that patches the PI app state with *pool*."""
    from contextlib import ExitStack

    from paper_ingestion.deps import get_db_pool as pi_get_db_pool
    from paper_ingestion.main import app as pi_app

    stack = ExitStack()
    stack.enter_context(patch_app_state(pi_app, {"db_pool": pool}))
    stack.enter_context(
        patch_dependency_overrides(
            pi_app,
            set_overrides={pi_get_db_pool: lambda: pool},
        )
    )
    return stack


def _make_config(
    api_key: str, le_url: str = "http://test", pi_url: str = "http://test"
) -> BotConfig:
    """Build a minimal BotConfig for the integration suite."""
    return BotConfig(
        telegram_token="dummy-token",
        telegram_chat_id=None,
        database_url="postgres://unused",
        learning_engine_url=le_url,
        paper_ingestion_url=pi_url,
        jarvis_api_key=SecretStr(api_key),
    )


def _le_client(pool: asyncpg.Pool, api_key: str) -> httpx.AsyncClient:
    """Async ASGI client routing to the LE app in-process."""
    from learning_engine.main import app as le_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=le_app),
        base_url="http://test",
        headers={"X-API-Key": api_key},
    )


def _pi_client(pool: asyncpg.Pool, api_key: str) -> httpx.AsyncClient:
    """Async ASGI client routing to the PI app in-process."""
    from paper_ingestion.main import app as pi_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pi_app),
        base_url="http://test",
        headers={"X-API-Key": api_key},
    )


# ---------------------------------------------------------------------------
# Mark all tests in this module
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.live_pg,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# T1 — fetch_tasks returns tasks with project_name
# ---------------------------------------------------------------------------


async def test_fetch_tasks_returns_project_name(_int_pool, _seed, monkeypatch):
    """services_client.fetch_tasks drives GET /api/tasks and returns tasks with project_name.

    Drift risk: the cross-project query uses a LEFT JOIN adding project_name.
    If the endpoint drops the JOIN or renames the column, fetch_tasks silently
    returns None for project_name and the bot's formatter mis-renders.
    """
    user_id = _seed["user_id"]
    task_id = _seed["task_id"]

    with configure_contract_api_key(monkeypatch) as key:
        with monkeypatch.context() as m:
            m.setattr("jarvis_common.auth._ip_in_allowlist", lambda _ip: True)

            config = _make_config(key)
            with _le_app_wired(_int_pool):
                async with _le_client(_int_pool, key) as http:
                    tasks = await fetch_tasks(http, config, user_id, limit=50)

    assert isinstance(tasks, list), f"fetch_tasks must return a list; got {type(tasks)}"
    assert len(tasks) >= 1, "Expected at least the seeded task"
    seeded = next((t for t in tasks if t["id"] == task_id), None)
    assert seeded is not None, f"Seeded task {task_id} not found in response"
    assert "project_name" in seeded, "TaskResponse must carry project_name key"
    assert seeded["project_name"] == "Integration Project", (
        f"project_name mismatch: {seeded['project_name']!r}"
    )
    assert seeded["status"] == "todo", f"Unexpected status: {seeded['status']!r}"


# ---------------------------------------------------------------------------
# T2 — fetch_upcoming_milestones returns name / deadline / project_name
# ---------------------------------------------------------------------------


async def test_fetch_upcoming_milestones_shape(_int_pool, _seed, monkeypatch):
    """services_client.fetch_upcoming_milestones drives GET /api/milestones/upcoming.

    Key assertions:
    - At least the seeded milestone appears
    - Each item has 'name', 'deadline' (parsed to datetime), 'project_name'
    - The bot's R5 isoformat→datetime coercion actually yields a datetime object

    Drift risk: if the endpoint removes project_name or the JOIN, the formatter
    cannot render the project context in the daily briefing.
    """
    user_id = _seed["user_id"]
    milestone_id = _seed["milestone_id"]

    with configure_contract_api_key(monkeypatch) as key:
        with monkeypatch.context() as m:
            m.setattr("jarvis_common.auth._ip_in_allowlist", lambda _ip: True)

            config = _make_config(key)
            with _le_app_wired(_int_pool):
                async with _le_client(_int_pool, key) as http:
                    items = await fetch_upcoming_milestones(http, config, user_id, within_days=7)

    assert isinstance(items, list), (
        f"fetch_upcoming_milestones must return a list; got {type(items)}"
    )
    assert len(items) >= 1, "Expected at least the seeded milestone (due in 2 days)"

    seeded = next((m for m in items if m["id"] == milestone_id), None)
    assert seeded is not None, f"Seeded milestone {milestone_id} not in response"
    assert "name" in seeded, "MilestoneDeadlineItem must have 'name'"
    assert "deadline" in seeded, "MilestoneDeadlineItem must have 'deadline'"
    assert "project_name" in seeded, "MilestoneDeadlineItem must have 'project_name'"
    assert seeded["name"] == "Integration Milestone"
    assert seeded["project_name"] == "Integration Project"
    # R5 coercion: services_client parses the ISO string back to datetime
    assert isinstance(seeded["deadline"], datetime), (
        f"deadline must be a datetime after R5 coercion; got {type(seeded['deadline'])}"
    )


# ---------------------------------------------------------------------------
# T3 — complete_task marks done and bumps daily_log.tasks_completed
# ---------------------------------------------------------------------------


async def test_complete_task_marks_done_and_bumps_daily_log(_int_pool, _seed, monkeypatch):
    """services_client.complete_task drives PUT /api/tasks/{id} with {status: done}.

    Verifies:
    - The response status is 'done'
    - The tasks table row status is 'done' (committed write)
    - daily_log.tasks_completed is incremented (BUG-1/S8 guard from the PR-C fix)

    Drift risk: if the endpoint signature changes (status key renamed, or
    completed_at/daily_log bump removed), the bot's done-marking silently
    degrades.
    """
    user_id = _seed["user_id"]
    task_id = _seed["task_id"]

    with configure_contract_api_key(monkeypatch) as key:
        with monkeypatch.context() as m:
            m.setattr("jarvis_common.auth._ip_in_allowlist", lambda _ip: True)

            config = _make_config(key)
            with _le_app_wired(_int_pool):
                async with _le_client(_int_pool, key) as http:
                    result = await complete_task(http, config, user_id, task_id)

    assert result is not None, "complete_task returned None (404) for existing task"
    assert result["status"] == "done", f"Expected status='done'; got {result['status']!r}"

    # Verify the DB write was committed (live pool, no per-test rollback)
    async with _int_pool.acquire() as conn:
        db_status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", task_id)
        assert db_status == "done", f"DB row not updated: status={db_status!r}"

        # BUG-1/S8: daily_log.tasks_completed must have been bumped
        tasks_completed = await conn.fetchval(
            "SELECT tasks_completed FROM daily_log WHERE user_id = $1 AND log_date = CURRENT_DATE",
            user_id,
        )
    assert tasks_completed is not None, "daily_log row missing after complete_task"
    assert tasks_completed >= 1, (
        f"tasks_completed should be ≥1 after one completion; got {tasks_completed}"
    )


# ---------------------------------------------------------------------------
# T4 — check_authors returns expected shape and matches the seeded author/paper
# ---------------------------------------------------------------------------


async def test_check_authors_returns_matches_shape(_int_pool, _seed, monkeypatch):
    """services_client.check_authors drives POST /api/authors/check.

    The endpoint matches papers created in the last 24 h against the caller's
    tracked_authors.  The seeded paper was inserted just now with a matching
    author name so at least one match is expected.

    Key assertions:
    - Response has 'matches', 'new_papers', 'authors_checked' keys
    - authors_checked == 1 (one tracked author seeded)
    - matches is a list; each entry has 'author_name' and 'papers'

    Drift risk: if check_authors renames response keys or the endpoint URL
    changes, the bot silently stops alerting on new author papers.

    Auth: the endpoint resolves identity via
    Depends(current_user_id_strict_with_owner_override). services_client sends
    X-Owner-User-Id + X-API-Key; we allowlist the source IP so the real
    owner-override path runs end-to-end (no resolver patching).
    """
    user_id = _seed["user_id"]

    with configure_contract_api_key(monkeypatch) as key:
        with monkeypatch.context() as m:
            m.setattr("jarvis_common.auth._ip_in_allowlist", lambda _ip: True)

            config = _make_config(key, pi_url="http://test")
            with _pi_app_wired(_int_pool):
                async with _pi_client(_int_pool, key) as http:
                    result = await check_authors(http, config, user_id)

    # Shape assertions (response keys the bot accesses)
    assert "matches" in result, f"check_authors response missing 'matches' key: {result.keys()}"
    assert "new_papers" in result, f"check_authors response missing 'new_papers': {result.keys()}"
    assert "authors_checked" in result, (
        f"check_authors response missing 'authors_checked': {result.keys()}"
    )
    assert isinstance(result["matches"], list), (
        f"'matches' must be a list; got {type(result['matches'])}"
    )
    assert result["authors_checked"] == 1, (
        f"Expected 1 tracked author; got {result['authors_checked']}"
    )
    # Each match entry must have author_name and papers
    for entry in result["matches"]:
        assert "author_name" in entry, f"Match entry missing 'author_name': {entry}"
        assert "papers" in entry, f"Match entry missing 'papers': {entry}"
