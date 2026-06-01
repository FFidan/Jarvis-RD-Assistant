from __future__ import annotations

import socket
import subprocess
from unittest.mock import patch

from jarvis_common.model_catalog import ModelCatalogEntry
from paper_ingestion.services.model_lifecycle import (
    HardwareInfo,
    _model_pull_job,
    _probe_macos_vram,
    async_get_cached_hardware,
    build_model_statuses,
    catalog_entry_for_model,
    compute_vram_fit,
    detect_hardware,
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
    """Catalog still ships at least one Ollama-installed but non-assignable
    embedding entry so this guard remains meaningful. Pre-2026-05-07 the
    fixture used ``qwen3-embedding:4b``; that entry was promoted to
    assignable=true when the production embed stack was upgraded.
    ``mxbai-embed-large`` and ``openai/text-embedding-3-small`` remain
    phase=future / assignable=false in the catalog and exercise the same code
    path."""
    statuses = build_model_statuses(
        installed=[{"name": "mxbai-embed-large", "size": 1, "details": {}}],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=2),
        cloud_api_keys={"openai": True},
    )
    by_id = {item["id"]: item for item in statuses}

    assert by_id["mxbai-embed-large"]["status"] == "pulled"
    assert by_id["mxbai-embed-large"]["can_assign"] is False
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


# ---------------------------------------------------------------------------
# compute_vram_fit tests (regression guard + edge cases)
# ---------------------------------------------------------------------------


def _hw_16gb() -> HardwareInfo:
    return HardwareInfo(
        vram_gb=16.0,
        vram_source="nvidia-smi",
        tier=2,
        detected_at="2026-05-07T00:00:00+00:00",
        machine_id="test-host",
    )


def _hw_zero() -> HardwareInfo:
    """Simulates a probe failure (CPU-only / no GPU detected)."""
    return HardwareInfo(
        vram_gb=0.0,
        vram_source="cpu",
        tier=0,
        detected_at="2026-05-07T00:00:00+00:00",
        machine_id="test-host",
    )


def test_compute_vram_fit_qwen3_14b_unfit_at_32768_on_16gb() -> None:
    """Regression guard: qwen3:14b at 32768 ctx should be unfit on 16 GB."""
    entry = catalog_entry_for_model("qwen3:14b")
    assert entry is not None, "qwen3:14b must be in the catalog"

    result = compute_vram_fit(entry, 32768, _hw_16gb())

    # Contract §4 sanity table: required ~19.83 GB > 1.20 * 16 = 19.2 GB → unfit
    assert result["default"] == "unfit", (
        f"Expected 'unfit' but got {result['default']!r}; "
        f"required_vram_gb={result['required_vram_gb']}"
    )
    assert result["at_num_ctx"] == 32768
    assert result["required_vram_gb"] is not None
    assert result["required_vram_gb"] > 19.0  # sanity: well above threshold
    assert result["base_vram_gb"] is not None
    assert result["base_vram_gb"] < result["required_vram_gb"]
    assert result["base_num_ctx"] == result["default_num_ctx"]


def test_compute_vram_fit_qwen3_14b_fits_at_8192_on_16gb() -> None:
    """qwen3:14b at 8192 ctx should fit on 16 GB."""
    entry = catalog_entry_for_model("qwen3:14b")
    assert entry is not None

    result = compute_vram_fit(entry, 8192, _hw_16gb())

    # Contract §4 sanity table: required ~10.0 GB ≤ 0.85 * 16 = 13.6 GB → fits
    assert result["default"] == "fits", (
        f"Expected 'fits' but got {result['default']!r}; "
        f"required_vram_gb={result['required_vram_gb']}"
    )
    assert result["at_num_ctx"] == 8192
    assert result["required_vram_gb"] is not None
    assert result["required_vram_gb"] < 13.6
    assert result["base_vram_gb"] == result["required_vram_gb"]
    assert result["base_num_ctx"] == 8192


def test_compute_vram_fit_falls_back_to_vram_gb_when_field_absent() -> None:
    """Entries without min_vram_gb_at_default_ctx fall back to entry.vram_gb."""
    # Build a minimal catalog entry with no hardware-aware fields set
    entry = ModelCatalogEntry(
        id="test/model",
        name="Test Model",
        provider="ollama",
        ollama_tag="test-model",
        roles=("smart",),
        vram_gb=8.0,
        disk_gb=5.0,
        context_tokens=16384,
        license="MIT",
        tier=2,
        description="test",
        notes="",
        last_reviewed="2026-05-07",
        # No hardware-aware fields → all None / defaults
    )

    result = compute_vram_fit(entry, 8192, _hw_16gb())

    # Falls back to vram_gb=8.0, default_ctx=min(8192,16384)=8192
    # required = 8.0 + max(0, 8192-8192) * 1024 / 1e9 = 8.0 GB
    # 8.0 ≤ 0.85 * 16 = 13.6 → fits
    assert result["default"] == "fits"
    assert result["required_vram_gb"] == 8.0
    assert result["base_vram_gb"] == 8.0
    assert result["base_num_ctx"] == 8192
    assert result["default_num_ctx"] == 8192
    assert result["kv_cache_bytes_per_token"] is None  # entry has None → returned as-is


def test_compute_vram_fit_skips_cloud_models() -> None:
    """Cloud models (provider != 'ollama') should return status 'cloud'."""
    entry = catalog_entry_for_model("anthropic/claude-sonnet-4-6")
    assert entry is not None, "anthropic/claude-sonnet-4-6 must be in the catalog"
    assert entry.provider == "anthropic"

    result = compute_vram_fit(entry, 8192, _hw_16gb())

    assert result["default"] == "cloud"
    assert result["required_vram_gb"] is None
    assert result["base_vram_gb"] is None
    assert result["base_num_ctx"] == 8192
    assert result["at_num_ctx"] == 8192


def test_compute_vram_fit_handles_zero_vram_probe_failure() -> None:
    """vram_gb == 0.0 (probe failure / CPU-only) → status 'unknown' for local models."""
    entry = catalog_entry_for_model("qwen3:14b")
    assert entry is not None

    result = compute_vram_fit(entry, 8192, _hw_zero())

    assert result["default"] == "unknown"
    assert result["required_vram_gb"] is None
    assert result["base_vram_gb"] is not None
    assert result["base_num_ctx"] == 8192


def test_machine_id_uses_hostname() -> None:
    """detect_hardware() must populate machine_id with socket.gethostname()."""
    hw = detect_hardware()
    assert hw.machine_id == socket.gethostname()


def test_build_model_statuses_includes_fit_detail() -> None:
    """Every entry in build_model_statuses output must carry a fit_detail dict."""
    statuses = build_model_statuses(
        installed=[{"name": "qwen3:4b", "size": 1, "details": {}}],
        current={"smart_model": "qwen3:4b"},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hw_16gb(),
        cloud_api_keys={},
    )
    for item in statuses:
        assert "fit_detail" in item, f"Entry {item.get('id')} missing fit_detail"
        fd = item["fit_detail"]
        assert "default" in fd
        assert fd["default"] in ("fits", "partial", "unfit", "cloud", "unknown")
        assert "at_num_ctx" in fd
        assert "base_vram_gb" in fd
        assert "base_num_ctx" in fd


def test_build_model_statuses_uses_num_ctx_per_role() -> None:
    """fit_detail.at_num_ctx reflects the per-role override when provided."""
    entry = catalog_entry_for_model("qwen3:14b")
    assert entry is not None

    # Provide a large num_ctx that pushes qwen3:14b into unfit territory
    statuses = build_model_statuses(
        installed=[{"name": "qwen3:14b", "size": 1, "details": {}}],
        current={"smart_model": "qwen3:14b"},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hw_16gb(),
        cloud_api_keys={},
        num_ctx_per_role={"smart": 32768},
    )
    by_id = {item["id"]: item for item in statuses}
    fd = by_id["qwen3:14b"]["fit_detail"]
    assert fd["at_num_ctx"] == 32768
    assert fd["base_num_ctx"] == (entry.default_num_ctx or 8192)
    assert fd["base_vram_gb"] == entry.min_vram_gb_at_default_ctx
    assert fd["required_vram_gb"] is not None
    assert fd["base_vram_gb"] is not None
    assert fd["required_vram_gb"] > fd["base_vram_gb"]
    assert fd["default"] == "unfit"


# ---------------------------------------------------------------------------
# DOM-J-07: async_get_cached_hardware uses asyncio.to_thread on cache miss
# ---------------------------------------------------------------------------


async def test_async_get_cached_hardware_uses_to_thread_on_cache_miss() -> None:
    """async_get_cached_hardware must delegate to asyncio.to_thread(detect_hardware)
    when no valid cache entry is present, ensuring the event loop is not blocked
    by the nvidia-smi subprocess call (DOM-J-07)."""
    fake_hw = _hw_16gb()

    async def _fake_to_thread(fn, *args, **kwargs):  # noqa: ARG001
        return fake_hw

    with patch(
        "paper_ingestion.services.model_lifecycle.asyncio.to_thread",
        side_effect=_fake_to_thread,
    ) as mock_to_thread:
        result = await async_get_cached_hardware(state=None)

    mock_to_thread.assert_called_once_with(detect_hardware)
    assert result is fake_hw


# _probe_macos_vram unit-awareness (parser must honour MB vs GB)


def _macos_proc(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def test_probe_macos_vram_parses_gb_unit() -> None:
    """A 'VRAM (Total): 8 GB' line reports 8.0 GB, not 8/1024."""
    with (
        patch("paper_ingestion.services.model_lifecycle.platform.system", return_value="Darwin"),
        patch(
            "paper_ingestion.services.model_lifecycle.subprocess.run",
            return_value=_macos_proc("        VRAM (Total): 8 GB\n"),
        ),
    ):
        assert _probe_macos_vram() == 8.0


def test_probe_macos_vram_converts_mb_to_gb() -> None:
    """A 'VRAM (Dynamic, Max): 1536 MB' line converts MB to 1.5 GB."""
    with (
        patch("paper_ingestion.services.model_lifecycle.platform.system", return_value="Darwin"),
        patch(
            "paper_ingestion.services.model_lifecycle.subprocess.run",
            return_value=_macos_proc("        VRAM (Dynamic, Max): 1536 MB\n"),
        ),
    ):
        assert _probe_macos_vram() == 1.5
