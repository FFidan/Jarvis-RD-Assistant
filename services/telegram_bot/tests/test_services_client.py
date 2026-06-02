"""Unit tests for telegram_bot.services_client.

Verifies:
- Correct URL on the right base service (learning_engine vs paper_ingestion)
- X-Owner-User-Id == str(user_id) and X-API-Key present in headers
- Correct query params / request body
- Parsed return values
- 404 → None for fetch_project and complete_task
- 5xx → raise_for_status raises httpx.HTTPStatusError that propagates
- fetch_new_paper_count sends an ISO date_from ≈ now-hours and returns total
- fetch_upcoming_milestones returns each deadline as a datetime
- fetch_due_card_count returns the due_now int
- check_authors returns the dict with matches/new_papers/authors_checked
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jarvis_common.testing_telegram import make_bot_config, make_http_response
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.services_client import (
    check_authors,
    complete_task,
    create_project,
    fetch_due_card_count,
    fetch_new_paper_count,
    fetch_project,
    fetch_project_milestones,
    fetch_project_tasks,
    fetch_projects,
    fetch_tasks,
    fetch_upcoming_milestones,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

USER_ID = 42
API_KEY = "test-api-key"


@pytest.fixture()
def config() -> BotConfig:
    """Minimal BotConfig for tests."""
    return make_bot_config(
        BotConfig,
        learning_engine_url="http://learn:8001",
        paper_ingestion_url="http://paper:8000",
        jarvis_api_key=SecretStr(API_KEY),
    )


def _make_http(response: MagicMock) -> AsyncMock:
    """Return an AsyncMock httpx.AsyncClient that returns *response* for any call."""
    http = AsyncMock(spec=httpx.AsyncClient)
    http.get = AsyncMock(return_value=response)
    http.post = AsyncMock(return_value=response)
    http.put = AsyncMock(return_value=response)
    return http


def _assert_owner_headers(call_kwargs: dict, user_id: int = USER_ID) -> None:
    """Assert the standard auth headers appear in *call_kwargs*."""
    headers = call_kwargs.get("headers", {})
    assert headers.get("X-Owner-User-Id") == str(user_id), (
        f"X-Owner-User-Id expected {user_id!r}, got {headers.get('X-Owner-User-Id')!r}"
    )
    assert headers.get("X-API-Key") == API_KEY, (
        f"X-API-Key expected {API_KEY!r}, got {headers.get('X-API-Key')!r}"
    )


# ---------------------------------------------------------------------------
# fetch_projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_projects_correct_url_and_headers(config: BotConfig) -> None:
    projects = [{"id": 1, "name": "P1"}, {"id": 2, "name": "P2"}]
    http = _make_http(make_http_response(projects))

    result = await fetch_projects(http, config, USER_ID)

    http.get.assert_called_once()
    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs.get("url", "")
    assert url == "http://learn:8001/api/projects"
    _assert_owner_headers(call_kwargs)
    assert result == projects


@pytest.mark.asyncio
async def test_fetch_projects_with_status_filter(config: BotConfig) -> None:
    http = _make_http(make_http_response([{"id": 1}]))

    await fetch_projects(http, config, USER_ID, status="active")

    _, call_kwargs = http.get.call_args
    assert call_kwargs.get("params") == {"status": "active"}


@pytest.mark.asyncio
async def test_fetch_projects_without_status_no_params(config: BotConfig) -> None:
    http = _make_http(make_http_response([]))

    await fetch_projects(http, config, USER_ID)

    _, call_kwargs = http.get.call_args
    # params should be None (not an empty dict) when no status is given
    assert call_kwargs.get("params") is None


@pytest.mark.asyncio
async def test_fetch_projects_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_projects(http, config, USER_ID)


# ---------------------------------------------------------------------------
# fetch_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_project_correct_url(config: BotConfig) -> None:
    project = {"id": 7, "name": "Proj7"}
    http = _make_http(make_http_response(project))

    result = await fetch_project(http, config, USER_ID, 7)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/projects/7"
    _assert_owner_headers(call_kwargs)
    assert result == project


@pytest.mark.asyncio
async def test_fetch_project_404_returns_none(config: BotConfig) -> None:
    resp = make_http_response({}, status=404)
    # For a 404, raise_for_status should NOT be called (we return None early)
    # so we need to allow the mock to not raise. Reconfigure raise_for_status
    # as a simple no-op to make the test robust:
    resp.raise_for_status = MagicMock()
    http = _make_http(resp)

    result = await fetch_project(http, config, USER_ID, 99)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_project_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=503))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_project(http, config, USER_ID, 1)


# ---------------------------------------------------------------------------
# fetch_project_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_project_tasks_correct_url(config: BotConfig) -> None:
    tasks = [{"id": 1, "title": "T1"}]
    http = _make_http(make_http_response(tasks))

    result = await fetch_project_tasks(http, config, USER_ID, 5)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/projects/5/tasks"
    _assert_owner_headers(call_kwargs)
    assert result == tasks


@pytest.mark.asyncio
async def test_fetch_project_tasks_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_project_tasks(http, config, USER_ID, 5)


# ---------------------------------------------------------------------------
# fetch_project_milestones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_project_milestones_correct_url(config: BotConfig) -> None:
    milestones = [{"id": 1, "name": "M1"}]
    http = _make_http(make_http_response(milestones))

    result = await fetch_project_milestones(http, config, USER_ID, 3)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/projects/3/milestones"
    _assert_owner_headers(call_kwargs)
    assert result == milestones


@pytest.mark.asyncio
async def test_fetch_project_milestones_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=502))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_project_milestones(http, config, USER_ID, 3)


# ---------------------------------------------------------------------------
# fetch_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_tasks_correct_url_and_default_limit(config: BotConfig) -> None:
    tasks = [{"id": 10, "title": "Task A"}]
    http = _make_http(make_http_response(tasks))

    result = await fetch_tasks(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/tasks"
    _assert_owner_headers(call_kwargs)
    params = call_kwargs.get("params", {})
    assert params.get("limit") == 50
    assert result == tasks


@pytest.mark.asyncio
async def test_fetch_tasks_with_status_and_project_id(config: BotConfig) -> None:
    http = _make_http(make_http_response([]))

    await fetch_tasks(http, config, USER_ID, status="in_progress", project_id=4, limit=10)

    _, call_kwargs = http.get.call_args
    params = call_kwargs.get("params", {})
    assert params.get("status") == "in_progress"
    assert params.get("project_id") == 4
    assert params.get("limit") == 10


@pytest.mark.asyncio
async def test_fetch_tasks_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_tasks(http, config, USER_ID)


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project_correct_url_and_body(config: BotConfig) -> None:
    created = {"id": 11, "name": "New Project"}
    http = _make_http(make_http_response(created))

    result = await create_project(http, config, USER_ID, name="New Project")

    http.post.assert_called_once()
    call_args, call_kwargs = http.post.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/projects"
    _assert_owner_headers(call_kwargs)
    body = call_kwargs.get("json", {})
    assert body.get("name") == "New Project"
    assert result == created


@pytest.mark.asyncio
async def test_create_project_with_description_and_deadline(config: BotConfig) -> None:
    http = _make_http(make_http_response({"id": 12}))

    await create_project(
        http, config, USER_ID, name="Proj", description="desc", deadline="2026-12-31"
    )

    _, call_kwargs = http.post.call_args
    body = call_kwargs.get("json", {})
    assert body.get("description") == "desc"
    assert body.get("deadline") == "2026-12-31"


@pytest.mark.asyncio
async def test_create_project_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await create_project(http, config, USER_ID, name="X")


# ---------------------------------------------------------------------------
# complete_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_task_correct_url_and_body(config: BotConfig) -> None:
    updated = {"id": 5, "status": "done"}
    http = _make_http(make_http_response(updated))

    result = await complete_task(http, config, USER_ID, 5)

    http.put.assert_called_once()
    call_args, call_kwargs = http.put.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/tasks/5"
    _assert_owner_headers(call_kwargs)
    # Body must be EXACTLY {"status": "done"}
    assert call_kwargs.get("json") == {"status": "done"}
    assert result == updated


@pytest.mark.asyncio
async def test_complete_task_404_returns_none(config: BotConfig) -> None:
    resp = make_http_response({}, status=404)
    resp.raise_for_status = MagicMock()  # not called on 404 path
    http = _make_http(resp)

    result = await complete_task(http, config, USER_ID, 999)

    assert result is None


@pytest.mark.asyncio
async def test_complete_task_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await complete_task(http, config, USER_ID, 1)


# ---------------------------------------------------------------------------
# fetch_upcoming_milestones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_upcoming_milestones_correct_url_and_params(config: BotConfig) -> None:
    raw_items = [
        {"id": 1, "name": "M1", "deadline": "2026-06-10T00:00:00"},
        {"id": 2, "name": "M2", "deadline": "2026-06-15T12:30:00+00:00"},
    ]
    http = _make_http(make_http_response(raw_items))

    await fetch_upcoming_milestones(http, config, USER_ID, within_days=7)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/milestones/upcoming"
    _assert_owner_headers(call_kwargs)
    assert call_kwargs.get("params") == {"within_days": 7}


@pytest.mark.asyncio
async def test_fetch_upcoming_milestones_deadline_parsed_as_datetime(config: BotConfig) -> None:
    """R5: deadline strings must be parsed to datetime objects."""
    raw_items = [
        {"id": 1, "name": "M1", "deadline": "2026-06-10T00:00:00"},
        {"id": 2, "name": "M2", "deadline": "2026-07-01T18:00:00+00:00"},
    ]
    http = _make_http(make_http_response(raw_items))

    result = await fetch_upcoming_milestones(http, config, USER_ID, within_days=30)

    for item in result:
        assert isinstance(item["deadline"], datetime), (
            f"Expected datetime, got {type(item['deadline'])!r} for {item['deadline']!r}"
        )


@pytest.mark.asyncio
async def test_fetch_upcoming_milestones_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_upcoming_milestones(http, config, USER_ID, within_days=7)


# ---------------------------------------------------------------------------
# fetch_due_card_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_due_card_count_correct_url_and_returns_int(config: BotConfig) -> None:
    stats = {"due_now": 13, "total_cards": 200}
    http = _make_http(make_http_response(stats))

    result = await fetch_due_card_count(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/stats"
    _assert_owner_headers(call_kwargs)
    assert result == 13
    assert isinstance(result, int)


@pytest.mark.asyncio
async def test_fetch_due_card_count_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=503))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_due_card_count(http, config, USER_ID)


# ---------------------------------------------------------------------------
# fetch_new_paper_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_new_paper_count_correct_url_and_returns_total(config: BotConfig) -> None:
    feed_resp = {"total": 7, "papers": []}
    http = _make_http(make_http_response(feed_resp))

    result = await fetch_new_paper_count(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/papers/feed"
    _assert_owner_headers(call_kwargs)
    assert result == 7
    assert isinstance(result, int)


@pytest.mark.asyncio
async def test_fetch_new_paper_count_date_from_is_iso_approx_now_minus_hours(
    config: BotConfig,
) -> None:
    """date_from sent to the API is the DATE of now - hours (day granularity)."""
    http = _make_http(make_http_response({"total": 0}))

    before = datetime.now(UTC)
    await fetch_new_paper_count(http, config, USER_ID, hours=24)
    after = datetime.now(UTC)

    _, call_kwargs = http.get.call_args
    params = call_kwargs.get("params", {})
    date_from_str = params.get("date_from", "")
    assert date_from_str, "date_from param must be present"

    # C2 regression guard: /api/papers/feed's date_from is a DATE param — a full
    # datetime ISO string is rejected with 422. Must be a pure date (no time).
    assert "T" not in date_from_str, f"date_from must be a date (no time), got {date_from_str!r}"

    # It is the calendar date of (now - hours). before/after differ only across
    # a midnight boundary, so accept either.
    expected_dates = {
        (before - timedelta(hours=24)).date().isoformat(),
        (after - timedelta(hours=24)).date().isoformat(),
    }
    assert date_from_str in expected_dates, (
        f"date_from {date_from_str!r} is not the date of now-24h ({expected_dates})"
    )


@pytest.mark.asyncio
async def test_fetch_new_paper_count_limit_param_is_1(config: BotConfig) -> None:
    """limit=1 is passed so the endpoint does minimum work for the count."""
    http = _make_http(make_http_response({"total": 3}))

    await fetch_new_paper_count(http, config, USER_ID)

    _, call_kwargs = http.get.call_args
    params = call_kwargs.get("params", {})
    assert params.get("limit") == 1


@pytest.mark.asyncio
async def test_fetch_new_paper_count_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_new_paper_count(http, config, USER_ID)


# ---------------------------------------------------------------------------
# check_authors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_authors_correct_url_and_headers(config: BotConfig) -> None:
    response_data = {"matches": 2, "new_papers": 5, "authors_checked": 10}
    http = _make_http(make_http_response(response_data))

    await check_authors(http, config, USER_ID)

    http.post.assert_called_once()
    call_args, call_kwargs = http.post.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/authors/check"
    _assert_owner_headers(call_kwargs)


@pytest.mark.asyncio
async def test_check_authors_returns_dict_with_expected_keys(config: BotConfig) -> None:
    response_data = {"matches": 2, "new_papers": 5, "authors_checked": 10}
    http = _make_http(make_http_response(response_data))

    result = await check_authors(http, config, USER_ID)

    assert result.get("matches") == 2
    assert result.get("new_papers") == 5
    assert result.get("authors_checked") == 10


@pytest.mark.asyncio
async def test_check_authors_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await check_authors(http, config, USER_ID)
