"""Live provider model listing: parsing, bounds, caching, and capability rules."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from jarvis_common.maintenance import OutboundEgressBlockedError
from jarvis_common.testing import FakeRecord
from paper_ingestion.services import provider_models
from paper_ingestion.services.llm_provider_registry import (
    PROVIDER_REGISTRY,
    provider_for_id,
)
from paper_ingestion.services.provider_models import (
    classify_live_model,
    fetch_all_provider_models,
    fetch_provider_models,
    models_url_for,
    reset_provider_model_cache,
)

_LOGGER = "paper_ingestion.services.provider_models"
_LOOPBACK_BASE_URL = "http://localhost:8000/v1"
_CUSTOM_BASE_URL_KEY = "llm.providers.custom_openai_compatible.base_url"
_CHAT_META = {"supportedGenerationMethods": ["generateContent"]}


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    reset_provider_model_cache()
    yield
    reset_provider_model_cache()


class _FakeConn:
    def __init__(self, config: dict[str, str]) -> None:
        self._config = config

    async def fetchrow(self, _sql: str, key: str) -> FakeRecord | None:
        if key not in self._config:
            return None
        return FakeRecord(value=self._config[key], encrypted_value=None)

    async def fetch(self, _sql: str, keys: list[str]) -> list[FakeRecord]:
        return [
            FakeRecord(key=key, value=value, encrypted_value=None)
            for key, value in self._config.items()
            if key in keys
        ]


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeConfigPool:
    """asyncpg.Pool-shaped stub backed by an in-memory user_config mapping."""

    def __init__(self, config: dict[str, str] | None = None) -> None:
        self.conn = _FakeConn(config or {})

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


class Recorder:
    """MockTransport handler that replays canned pages and records requests."""

    def __init__(self, *pages: Any, status: int = 200, content: bytes | None = None) -> None:
        self._pages = list(pages)
        self._status = status
        self._content = content
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._content is not None:
            return httpx.Response(self._status, content=self._content)
        if self._status >= 400:
            return httpx.Response(self._status, json={})
        index = min(len(self.requests) - 1, len(self._pages) - 1)
        return httpx.Response(self._status, json=self._pages[index])


def mock_http_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fetch(
    provider_id: str,
    handler: Any,
    *,
    config: dict[str, str] | None = None,
) -> provider_models.ProviderModelList:
    async with mock_http_client(handler) as client:
        return await fetch_provider_models(
            provider_id, db_pool=FakeConfigPool(config), http_client=client
        )


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def test_every_registry_provider_resolves_a_models_url() -> None:
    """All nine providers must have a list endpoint, including the custom one."""
    for provider in PROVIDER_REGISTRY:
        assert models_url_for(provider, _LOOPBACK_BASE_URL) is not None, provider.id


def test_custom_endpoint_without_base_url_has_no_models_url() -> None:
    assert models_url_for(provider_for_id("custom_openai_compatible"), None) is None


# ---------------------------------------------------------------------------
# Shape-agnostic parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": [{"id": "kimi-a"}]}, "kimi-a"),
        ({"models": [{"name": "kimi-b"}]}, "kimi-b"),
        ({"list": [{"model": "kimi-c"}]}, "kimi-c"),
        ({"data": ["kimi-d"]}, "kimi-d"),
        ({"models": [{"name": "models/kimi-e"}]}, "kimi-e"),
    ],
)
@pytest.mark.asyncio
async def test_parser_accepts_known_envelopes_and_id_keys(payload: Any, expected: str) -> None:
    listing = await _fetch("moonshot", Recorder(payload))

    assert [entry.name for entry in listing.entries] == [expected]
    assert listing.entries[0].id == f"moonshot/{expected}"


@pytest.mark.asyncio
async def test_unrecognised_envelope_yields_nothing_and_logs_observed_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        listing = await _fetch("moonshot", Recorder({"result": {"items": []}, "ok": True}))

    assert listing.entries == ()
    assert any("'ok', 'result'" in record.getMessage() for record in caplog.records), [
        record.getMessage() for record in caplog.records
    ]


@pytest.mark.asyncio
async def test_hostile_ids_are_dropped_and_counted() -> None:
    listing = await _fetch(
        "moonshot",
        Recorder(
            {
                "data": [
                    {"id": "kimi ok"},
                    {"id": "a/b/c"},
                    {"id": "kimi-\x07bell"},
                    {"id": "../../etc/passwd"},
                    {"id": "kimi-k2"},
                ]
            }
        ),
    )

    assert [entry.name for entry in listing.entries] == ["kimi-k2"]
    assert listing.excluded["invalid"] == 4


@pytest.mark.asyncio
async def test_oversized_body_is_refused_and_nothing_is_cached() -> None:
    # Valid JSON, so only the byte cap can refuse it.
    oversized = json.dumps({"pad": "x" * (2 * 1024 * 1024), "data": [{"id": "kimi-k2"}]})
    handler = Recorder(content=oversized.encode())

    listing = await _fetch("moonshot", handler)

    assert listing.entries == ()
    assert listing.error is not None and "size limit" in listing.error
    assert provider_models._cache == {}


@pytest.mark.asyncio
async def test_overlong_model_id_is_dropped_and_counted() -> None:
    listing = await _fetch(
        "moonshot", Recorder({"data": [{"id": "k" * 10_000}, {"id": "kimi-k2"}]})
    )

    assert [entry.name for entry in listing.entries] == ["kimi-k2"]
    assert listing.excluded["invalid"] == 1


@pytest.mark.asyncio
async def test_one_oversized_page_is_reported_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most providers return their whole list in one array with no pagination.

    Deciding truncation from the absence of a next page calls such a list
    complete while the cap silently drops its tail.
    """
    monkeypatch.setattr(provider_models, "_MAX_MODELS_PER_PROVIDER", 2)
    handler = Recorder({"data": [{"id": "kimi-a"}, {"id": "kimi-b"}, {"id": "kimi-c"}]})

    listing = await _fetch("moonshot", handler)

    assert [entry.name for entry in listing.entries] == ["kimi-a", "kimi-b"]
    assert listing.truncated is True
    assert len(handler.requests) == 1


@pytest.mark.asyncio
async def test_pagination_is_followed_and_truncates_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_models, "_MAX_MODELS_PER_PROVIDER", 2)
    handler = Recorder(
        {"data": [{"id": "kimi-a"}], "has_more": True, "last_id": "kimi-a"},
        {"data": [{"id": "kimi-b"}], "has_more": True, "last_id": "kimi-b"},
    )

    listing = await _fetch("moonshot", handler)

    assert [entry.name for entry in listing.entries] == ["kimi-a", "kimi-b"]
    assert listing.truncated is True
    assert handler.requests[1].url.params["after_id"] == "kimi-a"


@pytest.mark.asyncio
async def test_google_page_token_is_followed() -> None:
    handler = Recorder(
        {"models": [{"name": "models/gemini-a"}], "nextPageToken": "tok"},
        {"models": [{"name": "models/gemini-b"}]},
    )

    listing = await _fetch("google", handler)

    assert [entry.name for entry in listing.entries] == ["gemini-a", "gemini-b"]
    assert listing.truncated is False
    assert handler.requests[1].url.params["pageToken"] == "tok"


# ---------------------------------------------------------------------------
# Auth, egress guards, caching, and the sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyless_custom_endpoint_sends_no_authorization_header() -> None:
    handler = Recorder({"data": [{"id": "org/model"}]})

    listing = await _fetch(
        "custom_openai_compatible",
        handler,
        config={_CUSTOM_BASE_URL_KEY: _LOOPBACK_BASE_URL},
    )

    assert [entry.id for entry in listing.entries] == ["custom_openai/org/model"]
    assert "authorization" not in handler.requests[0].headers


@pytest.mark.asyncio
async def test_anthropic_uses_its_own_list_endpoint_and_headers() -> None:
    handler = Recorder({"data": [{"id": "claude-opus-4-1"}]})

    listing = await _fetch("anthropic", handler, config={"llm.anthropic.api_key": "secret-key"})

    request = handler.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/models"
    assert request.headers["x-api-key"] == "secret-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert [entry.id for entry in listing.entries] == ["anthropic/claude-opus-4-1"]


@pytest.mark.asyncio
async def test_egress_guard_is_consulted_and_quarantine_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _guard(label: str) -> None:
        calls.append(label)

    monkeypatch.setattr(provider_models, "ensure_outbound_egress_allowed", _guard)
    await _fetch("moonshot", Recorder({"data": []}))
    assert calls == ["cloud provider model list"]

    def _blocked(_label: str) -> None:
        raise OutboundEgressBlockedError("quarantine")

    reset_provider_model_cache()
    monkeypatch.setattr(provider_models, "ensure_outbound_egress_allowed", _blocked)
    with pytest.raises(OutboundEgressBlockedError):
        await _fetch("moonshot", Recorder({"data": []}))


@pytest.mark.asyncio
async def test_cache_is_served_until_the_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(provider_models, "_cache_clock", lambda: now[0])
    handler = Recorder({"data": [{"id": "kimi-k2"}]})

    async with mock_http_client(handler) as client:
        pool = FakeConfigPool()
        first = await fetch_provider_models("moonshot", db_pool=pool, http_client=client)
        now[0] += provider_models._CACHE_TTL_SECONDS - 1
        cached = await fetch_provider_models("moonshot", db_pool=pool, http_client=client)
        assert len(handler.requests) == 1
        assert cached.fetched_at == first.fetched_at

        now[0] += 2
        refreshed = await fetch_provider_models("moonshot", db_pool=pool, http_client=client)

    assert len(handler.requests) == 2
    assert refreshed.fetched_at != first.fetched_at


@pytest.mark.asyncio
async def test_concurrent_callers_issue_one_outbound_fetch() -> None:
    handler = Recorder({"data": [{"id": "kimi-k2"}]})

    async with mock_http_client(handler) as client:
        pool = FakeConfigPool()
        await asyncio.gather(
            *(fetch_provider_models("moonshot", db_pool=pool, http_client=client) for _ in range(4))
        )

    assert len(handler.requests) == 1


@pytest.mark.asyncio
async def test_failure_serves_the_stale_list_with_its_original_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(provider_models, "_cache_clock", lambda: now[0])
    pages: list[Any] = [{"data": [{"id": "kimi-k2"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        if pages:
            return httpx.Response(200, json=pages.pop())
        return httpx.Response(503, json={})

    async with mock_http_client(handler) as client:
        pool = FakeConfigPool()
        first = await fetch_provider_models("moonshot", db_pool=pool, http_client=client)
        now[0] += provider_models._CACHE_TTL_SECONDS + 1
        stale = await fetch_provider_models("moonshot", db_pool=pool, http_client=client)

    assert [entry.name for entry in stale.entries] == ["kimi-k2"]
    assert stale.fetched_at == first.fetched_at


@pytest.mark.asyncio
async def test_total_budget_expiry_yields_an_error_entry_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_models, "_TOTAL_FETCH_BUDGET_SECONDS", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"data": []})

    async with mock_http_client(handler) as client:
        results = await fetch_all_provider_models(
            ["moonshot"], db_pool=FakeConfigPool(), http_client=client
        )

    assert results["moonshot"].entries == ()
    assert results["moonshot"].error == "provider model list timed out"


@pytest.mark.asyncio
async def test_sweep_isolates_one_failing_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "moonshot" in str(request.url):
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]})

    async with mock_http_client(handler) as client:
        results = await fetch_all_provider_models(
            ["moonshot", "deepseek"], db_pool=FakeConfigPool(), http_client=client
        )

    assert results["moonshot"].error is not None
    assert [entry.id for entry in results["deepseek"].entries] == ["deepseek/deepseek-chat"]


# ---------------------------------------------------------------------------
# Capability classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_id", "model_id", "raw", "expected"),
    [
        ("google", "gemini-3-pro", {"supportedGenerationMethods": ["generateContent"]}, "chat"),
        ("google", "text-embedding-004", {"supportedGenerationMethods": ["embedContent"]}, "embed"),
        ("google", "veo-3", {"supportedGenerationMethods": ["predictLongRunning"]}, "unknown"),
        ("openai", "text-embedding-3-large", {}, "embed"),
        ("openai", "whisper-1", {}, "other"),
        ("openai", "gpt-5", {}, "chat"),
        ("openai", "o4-mini", {}, "chat"),
        ("openai", "sora-2", {}, "unknown"),
        ("anthropic", "claude-opus-4-1", {}, "chat"),
        ("openrouter", "vendor/model-x", {}, "chat"),
        ("custom_openai_compatible", "org/model-y", {}, "chat"),
        ("mistral", "mistral-large-latest", {}, "chat"),
        ("moonshot", "kimi-k2", {}, "chat"),
        ("zai", "glm-4.6", {}, "chat"),
        ("deepseek", "deepseek-chat", {}, "chat"),
    ],
)
def test_classify_live_model(
    provider_id: str, model_id: str, raw: dict[str, Any], expected: str
) -> None:
    assert classify_live_model(provider_id, model_id, raw) == expected


@pytest.mark.asyncio
async def test_chat_entries_are_assignable_for_both_generative_roles() -> None:
    listing = await _fetch(
        "google", Recorder({"models": [{"name": "models/gemini-3-pro"} | _CHAT_META]})
    )

    entry = listing.entries[0]
    assert entry.roles == ("smart", "fast")
    assert entry.assignable is True


@pytest.mark.asyncio
async def test_embedding_models_are_listed_but_never_offered_for_a_generative_role() -> None:
    listing = await _fetch(
        "openai", Recorder({"data": [{"id": "text-embedding-3-large"}, {"id": "gpt-5"}]})
    )

    by_name = {entry.name: entry for entry in listing.entries}
    embed_entry = by_name["text-embedding-3-large"]
    assert embed_entry.roles == ("embed",)
    assert "smart" not in embed_entry.roles and "fast" not in embed_entry.roles
    assert embed_entry.assignable is False
    assert by_name["gpt-5"].assignable is True


@pytest.mark.asyncio
async def test_non_chat_families_are_excluded_entirely_and_counted() -> None:
    listing = await _fetch("openai", Recorder({"data": [{"id": "whisper-1"}, {"id": "gpt-5"}]}))

    assert [entry.name for entry in listing.entries] == ["gpt-5"]
    assert listing.excluded["non_chat"] == 1


@pytest.mark.asyncio
async def test_unknown_capability_is_display_only_with_a_truthful_blocker() -> None:
    listing = await _fetch("openai", Recorder({"data": [{"id": "sora-2"}]}))

    entry = listing.entries[0]
    assert entry.assignable is False
    assert entry.notes == (
        "This provider did not say what this model can do, so JARVIS will not offer it for a role."
    )
    assert listing.excluded["unknown"] == 1


@pytest.mark.asyncio
async def test_a_model_already_in_the_bundled_catalog_is_not_listed_twice() -> None:
    listing = await _fetch("openai", Recorder({"data": [{"id": "gpt-4o"}, {"id": "gpt-5"}]}))

    assert [entry.id for entry in listing.entries] == ["openai/gpt-5"]


@pytest.mark.asyncio
async def test_synthesized_entries_carry_the_registry_provider_id() -> None:
    listing = await _fetch("zai", Recorder({"data": [{"id": "glm-4.6"}]}))

    assert listing.entries[0].provider == "zai"
    assert listing.fetched_at is not None
    assert listing.entries[0].last_reviewed == listing.fetched_at.date().isoformat()


def test_parse_model_items_ignores_a_non_object_payload() -> None:
    assert provider_models._parse_model_items(json.loads("[1, 2]")) == ([], [])
