from __future__ import annotations

from paper_ingestion.services.model_lifecycle import (
    HardwareInfo,
    _model_pull_job,
    build_model_statuses,
    catalog_entry_for_model,
    recommendations_for_role,
)


def _hardware(tier: int = 1) -> HardwareInfo:
    return HardwareInfo(
        vram_gb=8.0,
        vram_source="nvidia-smi",
        tier=tier,
        detected_at="2026-05-06T00:00:00+00:00",
    )


def test_build_model_statuses_uses_contract_enum_for_local_models() -> None:
    statuses = build_model_statuses(
        installed=[{"name": "qwen3:4b", "size": 1, "details": {}}],
        current={"smart_model": "qwen3:4b"},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=1),
        cloud_api_keys={},
    )
    by_id = {item["id"]: item for item in statuses}

    assert by_id["qwen3:4b"]["status"] == "active"
    assert by_id["qwen3:4b"]["can_assign"] is True
    assert by_id["qwen3:8b"]["status"] == "downloadable"
    assert by_id["qwen3:8b"]["can_assign"] is False
    assert by_id["qwen3:8b"]["assign_blocker"] == "Pull this model first."
    assert by_id["qwen3:30b-a3b"]["status"] == "unfit"


def test_build_model_statuses_tracks_cloud_key_presence() -> None:
    statuses = build_model_statuses(
        installed=[],
        current={"smart_model": "anthropic/claude-sonnet-4-6"},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=0),
        cloud_api_keys={"anthropic": True},
    )
    by_id = {item["id"]: item for item in statuses}

    assert by_id["anthropic/claude-sonnet-4-6"]["status"] == "cloud_active"
    assert by_id["anthropic/claude-sonnet-4-6"]["can_assign"] is True
    assert by_id["anthropic/claude-sonnet-4-6"]["provider_key_present"] is True
    assert by_id["openai/gpt-4o"]["status"] == "cloud_required"
    assert by_id["openai/gpt-4o"]["can_assign"] is False
    assert by_id["openai/gpt-4o"]["provider_key_present"] is False


def test_recommendations_for_role_sort_active_and_available_first() -> None:
    recommendations = recommendations_for_role(
        "smart",
        installed=[{"name": "qwen3:4b", "size": 1, "details": {}}],
        current={"smart_model": "qwen3:4b"},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=1),
        cloud_api_keys={"anthropic": True},
    )

    assert recommendations[0]["id"] == "qwen3:4b"
    assert any(item["id"] == "anthropic/claude-haiku-4-5" for item in recommendations)


def test_catalog_lookup_accepts_ollama_latest_suffix() -> None:
    assert catalog_entry_for_model("qwen3:4b:latest").id == "qwen3:4b"


def test_nonassignable_catalog_entries_surface_assignment_blocker() -> None:
    statuses = build_model_statuses(
        installed=[{"name": "qwen3-embedding:4b", "size": 1, "details": {}}],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=2),
        cloud_api_keys={"openai": True},
    )
    by_id = {item["id"]: item for item in statuses}

    assert by_id["qwen3-embedding:4b"]["status"] == "pulled"
    assert by_id["qwen3-embedding:4b"]["can_assign"] is False
    assert by_id["qwen3-embedding:4b"]["can_assign"] is False
    assert by_id["openai/text-embedding-3-small"]["can_assign"] is False


class _CancelledCtx:
    def __init__(self) -> None:
        self.messages: list[tuple[float, str | None]] = []
        self.calls = 0

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        self.messages.append((progress, message))

    async def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls > 1


class _StreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        yield '{"status":"pulling","completed":1,"total":10}'
        yield '{"status":"pulling","completed":2,"total":10}'


class _HTTPClient:
    def stream(self, *args, **kwargs):
        return _StreamResponse()


def test_tier4_entry_unfit_on_tier1_hardware() -> None:
    statuses = build_model_statuses(
        installed=[],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=1),
        cloud_api_keys={},
    )
    by_id = {item["id"]: item for item in statuses}
    assert by_id["qwen3:72b"]["status"] == "unfit"


async def test_model_pull_job_stops_when_cancelled_mid_stream() -> None:
    ctx = _CancelledCtx()

    try:
        await _model_pull_job(
            None,
            _HTTPClient(),
            {"ollama_tag": "qwen3:4b", "ollama_url": "http://ollama:11434"},
            ctx,
        )
    except RuntimeError as exc:
        assert "cancelled" in str(exc).lower()
    else:
        raise AssertionError("model pull should stop on cancellation")

    assert ctx.messages[-1][0] < 1.0
