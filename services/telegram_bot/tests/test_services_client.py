"""Unit tests for telegram_bot.services_client.

Verifies:
- Correct URL on the right base service (learning_engine vs paper_ingestion)
- X-Jarvis-Paired-User-Id == str(user_id) and no general API key in headers
- Correct query params / request body
- Parsed return values
- 404 → None for fetch_project and complete_task
- 5xx → raise_for_status raises httpx.HTTPStatusError that propagates
- fetch_new_paper_count sends today's UTC date as date_from and returns total
- fetch_upcoming_milestones returns each deadline as a datetime
- fetch_due_card_count returns the due_now int
- check_authors returns the dict with matches/new_papers/authors_checked
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jarvis_common.testing_telegram import make_bot_config, make_http_response
from telegram_bot.config import BotConfig
from telegram_bot.services_client import (
    PulsePayloadError,
    acknowledge_telegram_focus_completion,
    check_authors,
    complete_task,
    create_project,
    fetch_active_focus_session,
    fetch_due_card_count,
    fetch_inbox_count,
    fetch_new_paper_count,
    fetch_next_review_card,
    fetch_papers_feed,
    fetch_pending_telegram_focus_completion,
    fetch_project,
    fetch_project_milestones,
    fetch_project_tasks,
    fetch_projects,
    fetch_pulse_generation_status,
    fetch_pulse_today,
    fetch_stats,
    fetch_tasks,
    fetch_upcoming_milestones,
    fetch_weekly_digest,
    get_paper,
    pause_focus_session,
    record_paper_feedback,
    search_papers,
    search_papers_feed,
    start_focus_session,
    submit_review_rating,
    trigger_pulse_generation,
    update_paper_action,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

USER_ID = 42


def _focus_payload(*, state: str = "active", source: str = "telegram") -> dict:
    return {
        "id": 19,
        "state": state,
        "source": source,
        "duration_seconds": 1500,
        "remaining_seconds": 1500 if state != "completed" else 0,
        "started_at": "2026-08-09T12:00:00+00:00",
        "paused_at": "2026-08-09T12:05:00+00:00" if state == "paused" else None,
        "paused_seconds": 0.0,
        "completed_at": "2026-08-09T12:25:00+00:00" if state == "completed" else None,
        "recorded_seconds": 1500.0 if state == "completed" else 0.0,
        "task_id": None,
        "paper_id": None,
    }


def _pulse_deck_payload() -> dict:
    return {
        "deck_id": 7,
        "deck_date": "2026-08-09",
        "card_count": 1,
        "generated_at": "2026-08-09T06:00:00+00:00",
        "cards": [
            {
                "card_id": 11,
                "paper_id": 12,
                "paper_title": "Typed Pulse paper",
                "paper_authors": ["Researcher"],
                "paper_url": "https://example.org/paper",
                "rank": 1,
                "score": 0.8,
                "llm_relevance": 8,
                "llm_novelty": 7,
                "reasoning": "Relevant to the configured topic.",
                "signals": {"recency": 0.5},
                "reasoning_verified": False,
                "reasoning_confidence": "UNVERIFIED",
            }
        ],
        "stats": {},
    }


@pytest.fixture()
def config() -> BotConfig:
    """Minimal BotConfig for tests."""
    return make_bot_config(
        BotConfig,
        learning_engine_url="http://learn:8001",
        paper_ingestion_url="http://paper:8000",
    )


def _make_http(response: MagicMock) -> AsyncMock:
    """Return an AsyncMock httpx.AsyncClient that returns *response* for any call."""
    http = AsyncMock(spec=httpx.AsyncClient)
    http.get = AsyncMock(return_value=response)
    http.post = AsyncMock(return_value=response)
    http.put = AsyncMock(return_value=response)
    http.request = AsyncMock(return_value=response)
    return http


def _assert_owner_headers(call_kwargs: dict, user_id: int = USER_ID) -> None:
    """Assert only the local assertion-exchange marker is present."""
    headers = call_kwargs.get("headers", {})
    marker = "X-Jarvis-Paired-User-Id"
    assert headers.get(marker) == str(user_id), (
        f"{marker} expected {user_id!r}, got {headers.get(marker)!r}"
    )
    assert "X-API-Key" not in headers


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
async def test_fetch_new_paper_count_date_from_is_today_utc(
    config: BotConfig,
) -> None:
    """date_from is today's UTC date, so the briefing's "since midnight UTC" is literal."""
    http = _make_http(make_http_response({"total": 0}))

    before = datetime.now(UTC)
    await fetch_new_paper_count(http, config, USER_ID)
    after = datetime.now(UTC)

    _, call_kwargs = http.get.call_args
    params = call_kwargs.get("params", {})
    date_from_str = params.get("date_from", "")
    assert date_from_str, "date_from param must be present"

    # C2 regression guard: /api/papers/feed's date_from is a DATE param — a full
    # datetime ISO string is rejected with 422. Must be a pure date (no time).
    assert "T" not in date_from_str, f"date_from must be a date (no time), got {date_from_str!r}"

    # before/after differ only across a midnight boundary, so accept either.
    expected_dates = {before.date().isoformat(), after.date().isoformat()}
    assert date_from_str in expected_dates, (
        f"date_from {date_from_str!r} is not today's UTC date ({expected_dates})"
    )


@pytest.mark.asyncio
async def test_fetch_inbox_count_reads_the_inbox_view_total(config: BotConfig) -> None:
    """The inbox count comes from the inbox view's whole-view total, not a page length."""
    http = _make_http(make_http_response({"total": 473, "papers": []}))

    result = await fetch_inbox_count(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/papers/feed"
    _assert_owner_headers(call_kwargs)
    assert call_kwargs["params"] == {"view": "inbox", "limit": 1}
    assert result == 473


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


# ---------------------------------------------------------------------------
# fetch_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_stats_correct_url_and_full_payload(config: BotConfig) -> None:
    stats = {"total_cards": 200, "due_now": 13, "reviewed_today": 4, "streak_days": 2}
    http = _make_http(make_http_response(stats))

    result = await fetch_stats(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/stats"
    _assert_owner_headers(call_kwargs)
    assert result == stats
    assert "timeout" not in call_kwargs


@pytest.mark.asyncio
async def test_fetch_stats_explicit_timeout_forwarded(config: BotConfig) -> None:
    http = _make_http(make_http_response({"due_now": 0}))

    await fetch_stats(http, config, USER_ID, timeout=15.0)

    _, call_kwargs = http.get.call_args
    assert call_kwargs.get("timeout") == 15.0


@pytest.mark.asyncio
async def test_fetch_stats_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_stats(http, config, USER_ID)


# ---------------------------------------------------------------------------
# fetch_next_review_card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_next_review_card_correct_url_and_params(config: BotConfig) -> None:
    cards = [{"id": 1, "front": "Q"}]
    http = _make_http(make_http_response(cards))

    result = await fetch_next_review_card(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/review/next"
    assert call_kwargs.get("params") == {"limit": 1}
    _assert_owner_headers(call_kwargs)
    assert result == cards


@pytest.mark.asyncio
async def test_fetch_next_review_card_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_next_review_card(http, config, USER_ID)


# ---------------------------------------------------------------------------
# submit_review_rating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_review_rating_correct_url_and_body(config: BotConfig) -> None:
    result_payload = {"next_due_at": "2026-08-01T00:00:00"}
    http = _make_http(make_http_response(result_payload))

    result = await submit_review_rating(http, config, USER_ID, 42, 3)

    http.post.assert_called_once()
    call_args, call_kwargs = http.post.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://learn:8001/api/review/42"
    assert call_kwargs.get("json") == {"rating": 3}
    _assert_owner_headers(call_kwargs)
    assert result == result_payload


@pytest.mark.asyncio
async def test_submit_review_rating_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await submit_review_rating(http, config, USER_ID, 1, 2)


# ---------------------------------------------------------------------------
# focus sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_session_client_uses_durable_scoped_endpoints(config: BotConfig) -> None:
    http = _make_http(make_http_response(_focus_payload()))

    started = await start_focus_session(http, config, USER_ID, 1500)

    assert started.id == 19
    args, kwargs = http.post.call_args
    assert args[0] == "http://learn:8001/api/executive/focus/start"
    assert kwargs["json"] == {"duration_seconds": 1500, "source": "telegram"}
    _assert_owner_headers(kwargs)


@pytest.mark.asyncio
async def test_focus_session_client_decodes_active_pending_and_transition(
    config: BotConfig,
) -> None:
    http = _make_http(make_http_response(_focus_payload()))
    active = await fetch_active_focus_session(http, config, USER_ID)
    assert active is not None and active.state == "active"

    http.get.return_value = make_http_response(_focus_payload(state="completed"))
    pending = await fetch_pending_telegram_focus_completion(http, config, USER_ID)
    assert pending is not None and pending.recorded_seconds == 1500

    http.post.return_value = make_http_response(
        {"session": _focus_payload(state="paused"), "changed": True}
    )
    paused = await pause_focus_session(http, config, USER_ID, 19)
    assert paused.changed is True and paused.session.state == "paused"

    http.post.return_value = make_http_response(
        {"session": _focus_payload(state="completed"), "changed": True}
    )
    acknowledged = await acknowledge_telegram_focus_completion(http, config, USER_ID, 19)
    assert acknowledged.changed is True


@pytest.mark.asyncio
async def test_focus_session_client_rejects_malformed_payload(config: BotConfig) -> None:
    http = _make_http(make_http_response({"state": "active"}))

    with pytest.raises(ValueError, match="invalid focus session"):
        await fetch_active_focus_session(http, config, USER_ID)


# ---------------------------------------------------------------------------
# search_papers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_papers_correct_url_and_body(config: BotConfig) -> None:
    payload = {"results": [{"title": "P1"}], "total": 1, "per_source_counts": {"arxiv": 1}}
    http = _make_http(make_http_response(payload))

    result = await search_papers(http, config, USER_ID, "transformers")

    http.post.assert_called_once()
    call_args, call_kwargs = http.post.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/search"
    # The request model defaults to arXiv alone, so the four sources are explicit.
    assert call_kwargs.get("json") == {
        "query": "transformers",
        "source_types": ["arxiv", "semantic_scholar", "openalex", "pubmed"],
    }
    assert call_kwargs["timeout"] > 70.5
    _assert_owner_headers(call_kwargs)
    assert result == payload


@pytest.mark.asyncio
async def test_search_papers_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await search_papers(http, config, USER_ID, "q")


# ---------------------------------------------------------------------------
# fetch_papers_feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_papers_feed_correct_url_and_params(config: BotConfig) -> None:
    feed = {"papers": [{"id": 2}]}
    http = _make_http(make_http_response(feed))

    result = await fetch_papers_feed(http, config, USER_ID, view="inbox", limit=10)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/papers/feed"
    assert call_kwargs.get("params") == {"view": "inbox", "limit": 10}
    _assert_owner_headers(call_kwargs)
    assert result == feed


@pytest.mark.asyncio
async def test_fetch_papers_feed_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_papers_feed(http, config, USER_ID, view="library", limit=10)


@pytest.mark.asyncio
async def test_search_papers_feed_queries_the_library_view(config: BotConfig) -> None:
    feed = {"papers": [{"id": 2}], "total": 1, "search_mode": "bm25"}
    http = _make_http(make_http_response(feed))

    result = await search_papers_feed(http, config, USER_ID, "transformers")

    http.post.assert_not_called()
    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/papers/feed"
    assert call_kwargs.get("params") == {"view": "library", "limit": 10, "q": "transformers"}
    _assert_owner_headers(call_kwargs)
    assert result == feed


# ---------------------------------------------------------------------------
# get_paper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_correct_url(config: BotConfig) -> None:
    detail = {"paper": {"id": 9}, "summary": {"tldr": "x"}}
    http = _make_http(make_http_response(detail))

    result = await get_paper(http, config, USER_ID, 9)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/papers/9"
    _assert_owner_headers(call_kwargs)
    assert result == detail


@pytest.mark.asyncio
async def test_get_paper_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await get_paper(http, config, USER_ID, 1)


# ---------------------------------------------------------------------------
# update_paper_action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_paper_action_correct_method_and_url(config: BotConfig) -> None:
    http = _make_http(make_http_response({}))

    await update_paper_action(http, config, USER_ID, 7, ("PUT", "trash"))

    http.request.assert_called_once()
    call_args, call_kwargs = http.request.call_args
    assert call_args[0] == "PUT"
    assert call_args[1] == "http://paper:8000/api/papers/7/trash"
    _assert_owner_headers(call_kwargs)


@pytest.mark.asyncio
async def test_update_paper_action_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await update_paper_action(http, config, USER_ID, 1, ("PUT", "save"))


# ---------------------------------------------------------------------------
# record_paper_feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_paper_feedback_correct_url_and_body(config: BotConfig) -> None:
    http = _make_http(make_http_response({}))

    await record_paper_feedback(
        http, config, USER_ID, 3, {"signal": "positive", "source": "pulse_thumbs"}
    )

    http.post.assert_called_once()
    call_args, call_kwargs = http.post.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/papers/3/feedback"
    assert call_kwargs.get("json") == {"signal": "positive", "source": "pulse_thumbs"}
    _assert_owner_headers(call_kwargs)


@pytest.mark.asyncio
async def test_record_paper_feedback_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await record_paper_feedback(
            http, config, USER_ID, 1, {"signal": "negative", "source": "feed_thumbs"}
        )


# ---------------------------------------------------------------------------
# fetch_pulse_today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_pulse_today_no_limit_omits_params(config: BotConfig) -> None:
    deck = _pulse_deck_payload()
    http = _make_http(make_http_response(deck))

    result = await fetch_pulse_today(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/pulse/today"
    assert call_kwargs.get("params") is None
    _assert_owner_headers(call_kwargs)
    assert result is not None
    assert result.deck_id == 7
    assert result.cards[0].paper_id == 12


@pytest.mark.asyncio
async def test_fetch_pulse_today_with_limit(config: BotConfig) -> None:
    http = _make_http(make_http_response(_pulse_deck_payload()))

    await fetch_pulse_today(http, config, USER_ID, limit=1)

    _, call_kwargs = http.get.call_args
    assert call_kwargs.get("params") == {"limit": 1}


@pytest.mark.asyncio
async def test_fetch_pulse_today_null_body_returns_none(config: BotConfig) -> None:
    """The endpoint responds 200 + JSON null when no deck exists for today."""
    http = _make_http(make_http_response(None))

    result = await fetch_pulse_today(http, config, USER_ID)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_pulse_today_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_pulse_today(http, config, USER_ID)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_score",
        "count_mismatch",
        "stale_without_age",
        "unknown_confidence",
        "false_empty",
        "invalid_date",
        "naive_timestamp",
    ],
)
@pytest.mark.asyncio
async def test_fetch_pulse_today_rejects_malformed_payload_without_echo(
    config: BotConfig,
    case: str,
) -> None:
    payload = _pulse_deck_payload()
    payload["cards"][0]["paper_title"] = "private title"
    if case == "wrong_score":
        payload["cards"][0]["score"] = "not-a-number"
    elif case == "count_mismatch":
        payload["card_count"] = 2
    elif case == "stale_without_age":
        payload["is_stale"] = True
    elif case == "unknown_confidence":
        payload["cards"][0]["reasoning_confidence"] = "CERTAIN"
    elif case == "false_empty":
        payload["empty_reason"] = "no_data_yet"
    elif case == "invalid_date":
        payload["deck_date"] = "2026-99-99"
    else:
        payload["generated_at"] = "2026-08-09T06:00:00"
    http = _make_http(make_http_response(payload))

    with pytest.raises(PulsePayloadError) as exc_info:
        await fetch_pulse_today(http, config, USER_ID)

    assert str(exc_info.value) == "Pulse response did not match the expected contract"
    assert "private title" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# trigger_pulse_generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_pulse_generation_correct_url(config: BotConfig) -> None:
    http = _make_http(make_http_response({"job_id": "x", "status": "queued"}))

    job = await trigger_pulse_generation(http, config, USER_ID)

    http.post.assert_called_once()
    call_args, call_kwargs = http.post.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/pulse/generate"
    _assert_owner_headers(call_kwargs)
    assert job.job_id == "x"


@pytest.mark.asyncio
async def test_trigger_pulse_generation_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await trigger_pulse_generation(http, config, USER_ID)


@pytest.mark.asyncio
async def test_trigger_pulse_generation_rejects_missing_job_identity(config: BotConfig) -> None:
    http = _make_http(make_http_response({"status": "queued"}))

    with pytest.raises(PulsePayloadError, match="job response"):
        await trigger_pulse_generation(http, config, USER_ID)


# ---------------------------------------------------------------------------
# fetch_pulse_generation_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_pulse_generation_status_correct_url(config: BotConfig) -> None:
    http = _make_http(make_http_response({"job_id": "job-1", "status": "running"}))

    status = await fetch_pulse_generation_status(http, config, USER_ID, "job-1")

    http.get.assert_called_once()
    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/pulse/generate/job-1"
    _assert_owner_headers(call_kwargs)
    assert status.status == "running"
    assert status.is_terminal is False


@pytest.mark.asyncio
async def test_fetch_pulse_generation_status_rejects_unknown_status(config: BotConfig) -> None:
    http = _make_http(make_http_response({"job_id": "job-1", "status": "almost"}))

    with pytest.raises(PulsePayloadError, match="job status"):
        await fetch_pulse_generation_status(http, config, USER_ID, "job-1")


# ---------------------------------------------------------------------------
# fetch_weekly_digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_weekly_digest_correct_url_and_params(config: BotConfig) -> None:
    digest = {"topics": [{"name": "NLP"}], "total_papers": 4}
    http = _make_http(make_http_response(digest))

    result = await fetch_weekly_digest(http, config, USER_ID)

    call_args, call_kwargs = http.get.call_args
    url = call_args[0] if call_args else call_kwargs["url"]
    assert url == "http://paper:8000/api/digest/weekly"
    assert call_kwargs.get("params") == {"days": 7}
    _assert_owner_headers(call_kwargs)
    assert result == digest


@pytest.mark.asyncio
async def test_fetch_weekly_digest_5xx_propagates(config: BotConfig) -> None:
    http = _make_http(make_http_response({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_weekly_digest(http, config, USER_ID)


# ---------------------------------------------------------------------------
# Client boundary -- auth and backend transport stay confined to this module
# ---------------------------------------------------------------------------

_AWAITED_TRANSPORT_METHODS = frozenset(
    {"get", "options", "head", "post", "put", "patch", "delete", "request", "send"}
)
_STREAM_TRANSPORT_METHOD = "stream"


def _outbound_transport_calls(source: str) -> list[tuple[int, str]]:
    """Return direct async HTTP-style calls that bypass the typed client."""
    calls: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            function = node.value.func
            if isinstance(function, ast.Attribute) and function.attr in _AWAITED_TRANSPORT_METHODS:
                calls.append((node.lineno, function.attr))
        elif isinstance(node, ast.AsyncWith):
            for item in node.items:
                context = item.context_expr
                if (
                    isinstance(context, ast.Call)
                    and isinstance(context.func, ast.Attribute)
                    and context.func.attr == _STREAM_TRANSPORT_METHOD
                ):
                    calls.append((context.lineno, context.func.attr))
    return sorted(calls)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "async def bypass(http):\n"
            '    return await http.get("http://learning:8001/api/stats")\n',
            [(2, "get")],
        ),
        (
            "async def bypass(http, endpoint):\n    return await http.post(endpoint)\n",
            [(2, "post")],
        ),
        (
            'async def bypass(http, endpoint):\n    return await http.request("GET", endpoint)\n',
            [(2, "request")],
        ),
        (
            "async def bypass(http, request):\n    return await http.send(request)\n",
            [(2, "send")],
        ),
        (
            "async def bypass(http, endpoint):\n"
            '    async with http.stream("GET", endpoint) as response:\n'
            "        return await response.aread()\n",
            [(2, "stream")],
        ),
        (
            "async def allowed(mapping, bot):\n"
            '    value = mapping.get("key")\n'
            '    await bot.send_message("done")\n'
            "    return value\n",
            [],
        ),
    ],
    ids=(
        "hardcoded-url",
        "indirect-endpoint",
        "generic-request",
        "prepared-send",
        "streamed-request",
        "non-transport-lookalikes",
    ),
)
def test_outbound_transport_detector_catches_boundary_bypasses(
    source: str, expected: list[tuple[int, str]]
) -> None:
    assert _outbound_transport_calls(source) == expected


def test_backend_transport_confined_to_client_boundary() -> None:
    """Auth headers and backend service URLs must stay inside the typed client.

    Every backend call the bot makes must go through this module's typed
    functions; a handler or orchestrator building headers directly bypasses
    the auth-contract coverage those functions carry.  Scanning only the
    package source deliberately excludes tests and documentation.
    """
    package_root = Path(__file__).resolve().parents[1] / "telegram_bot"
    services_client_path = package_root / "services_client.py"
    allowed = {
        package_root / "config.py",
        package_root / "platform_client.py",
        package_root / "service_auth.py",
        services_client_path,
    }
    forbidden_markers = ("_owner_headers", ".learning_engine_url", ".paper_ingestion_url")
    offenders: dict[str, list[str]] = {}
    for path in sorted(package_root.rglob("*.py")):
        source = path.read_text()
        violations: list[str] = []
        if path not in allowed:
            violations.extend(marker for marker in forbidden_markers if marker in source)
        if path not in allowed:
            violations.extend(
                f"outbound .{method}() at line {line}"
                for line, method in _outbound_transport_calls(source)
            )
        if violations:
            offenders[str(path.relative_to(package_root))] = violations
    assert not offenders, (
        f"backend auth or service URL referenced outside services_client.py: {offenders}"
    )
