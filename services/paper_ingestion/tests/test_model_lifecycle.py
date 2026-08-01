from __future__ import annotations

import os
import socket
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from jarvis_common.maintenance import OutboundEgressBlockedError
from jarvis_common.model_catalog import ModelCatalogEntry
from paper_ingestion.services.model_lifecycle import (
    MODEL_CATALOG,
    NUM_CTX_LADDER,
    HardwareInfo,
    ModelStatusDict,
    _DEFAULT_KV_CACHE_BYTES_PER_TOKEN,
    _model_pull_job,
    _probe_macos_vram,
    async_get_cached_hardware,
    build_model_statuses,
    catalog_entry_for_model,
    compute_vram_fit,
    detect_hardware,
    fits_with_embed_reserve,
    hardware_tier,
    recommendations_for_role,
    safe_num_ctx,
)
from paper_ingestion.services.provider_models import live_model_entry


def _hardware(tier: int = 1) -> HardwareInfo:
    return HardwareInfo(
        vram_gb=8.0,
        vram_source="nvidia-smi",
        tier=tier,
        detected_at="2026-05-06T00:00:00+00:00",
        vram_source_detail="GPU detected inside the container",
        host_gpu_divergence=False,
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
    ``mxbai-embed-large``, ``openai/text-embedding-3-small``, and
    ``gpt-oss:20b`` remain phase=future / assignable=false in the catalog and
    exercise the same code path."""
    statuses = build_model_statuses(
        installed=[
            {"name": "mxbai-embed-large", "size": 1, "details": {}},
            {"name": "gpt-oss:20b", "size": 1, "details": {}},
        ],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=2),
        cloud_api_keys={"openai": True},
    )
    by_id = {item["id"]: item for item in statuses}

    assert by_id["mxbai-embed-large"]["status"] == "pulled"
    assert by_id["mxbai-embed-large"]["can_assign"] is False
    assert by_id["gpt-oss:20b"]["status"] == "pulled"
    assert by_id["gpt-oss:20b"]["can_assign"] is False
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


class _RecordingHTTPClient:
    def __init__(self) -> None:
        self.stream_calls = 0

    def stream(self, *args, **kwargs):
        self.stream_calls += 1
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


class _NeverCancelledCtx:
    def __init__(self) -> None:
        self.messages: list[tuple[float, str | None]] = []

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        self.messages.append((progress, message))

    async def is_cancelled(self) -> bool:
        return False


class _QuarantineOnProgressCtx(_NeverCancelledCtx):
    def __init__(self, quarantine_path) -> None:
        super().__init__()
        self._quarantine_path = quarantine_path

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        await super().update_progress(progress, message)
        if progress == 0.0:
            self._quarantine_path.touch()


class _ErrorStreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        yield '{"status":"pulling","error":"disk full"}'


class _ErrorHTTPClient:
    def stream(self, *args, **kwargs):
        return _ErrorStreamResponse()


async def test_model_pull_job_reports_progress_and_completes() -> None:
    ctx = _NeverCancelledCtx()

    result = await _model_pull_job(
        None,
        _HTTPClient(),
        {"ollama_tag": "qwen3:4b", "ollama_url": "http://ollama:11434"},
        ctx,
    )

    assert result == {"tag": "qwen3:4b", "status": "pulled", "message": "pulling"}
    assert ctx.messages == [
        (0.0, "Starting pull for qwen3:4b"),
        (0.1, "pulling"),
        (0.2, "pulling"),
        (1.0, "Done"),
    ]


async def test_model_pull_job_rechecks_quarantine_before_opening_stream(
    tmp_path, monkeypatch
) -> None:
    """Refuse a quarantine that begins after the job starts but before HTTP."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    client = _RecordingHTTPClient()
    ctx = _QuarantineOnProgressCtx(quarantine)

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        await _model_pull_job(
            None,
            client,
            {"ollama_tag": "qwen3:4b", "ollama_url": "http://ollama:11434"},
            ctx,
        )

    assert client.stream_calls == 0


async def test_model_pull_job_raises_on_stream_error_event() -> None:
    ctx = _NeverCancelledCtx()

    with pytest.raises(RuntimeError, match="disk full"):
        await _model_pull_job(
            None,
            _ErrorHTTPClient(),
            {"ollama_tag": "qwen3:4b", "ollama_url": "http://ollama:11434"},
            ctx,
        )

    assert ctx.messages == [
        (0.0, "Starting pull for qwen3:4b"),
        (0.05, "pulling"),
        (0.0, "Failed: disk full"),
    ]


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
        vram_source_detail="GPU detected inside the container",
        host_gpu_divergence=False,
    )


def _hw_zero() -> HardwareInfo:
    """Simulates a probe failure (CPU-only / no GPU detected)."""
    return HardwareInfo(
        vram_gb=0.0,
        vram_source="cpu",
        tier=0,
        detected_at="2026-05-07T00:00:00+00:00",
        machine_id="test-host",
        vram_source_detail="no GPU detected — running on CPU",
        host_gpu_divergence=False,
    )


def test_compute_vram_fit_qwen3_14b_unfit_at_32768_on_16gb() -> None:
    """Regression guard: qwen3:14b at 32768 ctx should be unfit on 16 GB."""
    entry = catalog_entry_for_model("qwen3:14b")
    assert entry is not None, "qwen3:14b must be in the catalog"

    result = compute_vram_fit(entry, 32768, _hw_16gb())

    # Sanity check: required ~19.83 GB > 1.20 * 16 = 19.2 GB → unfit
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

    # Sanity check: required ~10.0 GB ≤ 0.85 * 16 = 13.6 GB → fits
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
# async_get_cached_hardware uses asyncio.to_thread on cache miss
# ---------------------------------------------------------------------------


async def test_async_get_cached_hardware_uses_to_thread_on_cache_miss() -> None:
    """async_get_cached_hardware must delegate to asyncio.to_thread(detect_hardware)
    when no valid cache entry is present, ensuring the event loop is not blocked
    by the nvidia-smi subprocess call."""
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


# ---------------------------------------------------------------------------
# JARVIS_HOST_VRAM_MB env override (F-MODEL-1)
# ---------------------------------------------------------------------------


def test_detect_hardware_host_env_overrides_vram() -> None:
    """JARVIS_HOST_VRAM_MB set to a valid positive integer → vram_source='host-env'."""
    with (
        patch.dict("os.environ", {"JARVIS_HOST_VRAM_MB": "24576"}),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=None),
    ):
        hw = detect_hardware()

    assert hw.vram_source == "host-env"
    assert hw.vram_gb == round(24576 / 1024.0, 1)
    assert hw.tier > 0
    assert hw.vram_source_detail == "GPU detected on the host at install time"


def test_detect_hardware_host_env_divergence_when_container_blind() -> None:
    """JARVIS_HOST_VRAM_MB set + in-container probe finds nothing → host_gpu_divergence=True."""
    with (
        patch.dict("os.environ", {"JARVIS_HOST_VRAM_MB": "8192"}),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=None),
    ):
        hw = detect_hardware()

    assert hw.host_gpu_divergence is True
    assert hw.vram_source == "host-env"


def test_detect_hardware_no_divergence_when_container_has_gpu() -> None:
    """JARVIS_HOST_VRAM_MB set + container probe succeeds → host_gpu_divergence=False."""
    with (
        patch.dict("os.environ", {"JARVIS_HOST_VRAM_MB": "8192"}),
        patch(
            "paper_ingestion.services.model_lifecycle._probe_nvidia_smi",
            return_value=8.0,
        ),
    ):
        hw = detect_hardware()

    assert hw.host_gpu_divergence is False
    assert hw.vram_source == "host-env"


def test_detect_hardware_env_absent_unchanged() -> None:
    """No JARVIS_HOST_VRAM_MB → normal probe path, host_gpu_divergence=False."""
    env_without_override = {k: v for k, v in os.environ.items() if k != "JARVIS_HOST_VRAM_MB"}
    with (
        patch.dict("os.environ", env_without_override, clear=True),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=16.0),
    ):
        hw = detect_hardware()

    assert hw.vram_source == "nvidia-smi"
    assert hw.host_gpu_divergence is False
    assert hw.vram_source_detail == "GPU detected inside the container"


def test_detect_hardware_env_garbage_ignored() -> None:
    """Garbage / zero / negative JARVIS_HOST_VRAM_MB values fall back to normal probe."""
    for bad in ("abc", "0", "-5", ""):
        with (
            patch.dict("os.environ", {"JARVIS_HOST_VRAM_MB": bad}),
            patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=8.0),
        ):
            hw = detect_hardware()
        assert hw.vram_source == "nvidia-smi", f"bad={bad!r} should be ignored"
        assert hw.host_gpu_divergence is False


# ---------------------------------------------------------------------------
# JARVIS_GPU_VENDOR (HardwareInfo.vendor + divergence suppression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vendor", ["amd", "intel"])
def test_detect_hardware_amd_intel_host_suppresses_divergence(vendor: str) -> None:
    """The in-container probe is nvidia-only, so it is structurally blind on
    AMD/Intel hosts — the no-GPU divergence warning must not fire there."""
    with (
        patch.dict(
            "os.environ",
            {"JARVIS_HOST_VRAM_MB": "16368", "JARVIS_GPU_VENDOR": vendor},
        ),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=None),
    ):
        hw = detect_hardware()

    assert hw.vendor == vendor
    assert hw.host_gpu_divergence is False
    assert hw.vram_source == "host-env"
    assert hw.tier > 0


def test_detect_hardware_nvidia_host_keeps_divergence() -> None:
    """An NVIDIA host with a blind container probe still flags the missing overlay."""
    with (
        patch.dict(
            "os.environ",
            {"JARVIS_HOST_VRAM_MB": "8192", "JARVIS_GPU_VENDOR": "nvidia"},
        ),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=None),
    ):
        hw = detect_hardware()

    assert hw.vendor == "nvidia"
    assert hw.host_gpu_divergence is True


def test_detect_hardware_legacy_env_without_vendor_keeps_divergence() -> None:
    """Pre-vendor .env files (JARVIS_HOST_VRAM_MB set, no JARVIS_GPU_VENDOR)
    preserve the original divergence behavior."""
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_GPU_VENDOR"}
    env["JARVIS_HOST_VRAM_MB"] = "8192"
    with (
        patch.dict("os.environ", env, clear=True),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=None),
    ):
        hw = detect_hardware()

    assert hw.vendor == "none"
    assert hw.host_gpu_divergence is True


def test_detect_hardware_vendor_inferred_from_container_probe() -> None:
    """No vendor env: a successful in-container nvidia probe implies nvidia."""
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_GPU_VENDOR"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=16.0),
    ):
        hw = detect_hardware()

    assert hw.vendor == "nvidia"


def test_detect_hardware_vendor_invalid_env_ignored() -> None:
    """An unrecognized JARVIS_GPU_VENDOR value falls back to inference."""
    with (
        patch.dict(
            "os.environ",
            {"JARVIS_HOST_VRAM_MB": "8192", "JARVIS_GPU_VENDOR": "matrox"},
        ),
        patch("paper_ingestion.services.model_lifecycle._probe_nvidia_smi", return_value=None),
    ):
        hw = detect_hardware()

    assert hw.vendor == "none"
    assert hw.host_gpu_divergence is True


# ---------------------------------------------------------------------------
# safe_num_ctx (D9) — largest slider stop that fits beside the embed model.
# All expectations are derived from model_catalog.json numbers (qwen3:8b:
# min_vram 6.0 GB, kv 250000 B/token, default 8192, max 32768; embed reserve =
# qwen3-embedding:4b vram_gb 3.0).
# ---------------------------------------------------------------------------


def _hw_with_vram(vram_gb: float) -> HardwareInfo:
    return HardwareInfo(
        vram_gb=vram_gb,
        vram_source="nvidia-smi" if vram_gb > 0.0 else "cpu",
        tier=hardware_tier(vram_gb),
        detected_at="2026-06-12T00:00:00+00:00",
    )


def _embed_reserve_gb() -> float:
    return catalog_entry_for_model("qwen3-embedding:4b").vram_gb


def test_safe_num_ctx_24gb_qwen3_8b_reaches_catalog_max() -> None:
    """24 GB box: 6.0 + (32768-8192)*250000/1e9 + 3.0 = 15.144 ≤ 19.2 → 32768
    (= max; the next ladder stop 65536 exceeds max_num_ctx)."""
    entry = catalog_entry_for_model("qwen3:8b")
    assert safe_num_ctx(entry, _hw_with_vram(24.0), _embed_reserve_gb()) == 32768


def test_safe_num_ctx_16gb_qwen3_8b_picks_16k() -> None:
    """16 GB box: 16384 needs 6.0+2.048+3.0 = 11.048 ≤ 12.8; 32768 needs 15.144 > 12.8."""
    entry = catalog_entry_for_model("qwen3:8b")
    assert safe_num_ctx(entry, _hw_with_vram(16.0), _embed_reserve_gb()) == 16384


def test_safe_num_ctx_cpu_returns_catalog_default() -> None:
    """vram_gb == 0.0 (CPU / probe failed) → catalog default_num_ctx, no ladder walk."""
    entry = catalog_entry_for_model("qwen3:8b")
    assert safe_num_ctx(entry, _hw_with_vram(0.0), _embed_reserve_gb()) == entry.default_num_ctx


def test_safe_num_ctx_tiny_vram_floors_at_smallest_stop() -> None:
    """6 GB box: even 2048 needs the full base residency 6.0+3.0 = 9.0 > 4.8 —
    floor at the smallest ladder stop, never below it."""
    entry = catalog_entry_for_model("qwen3:8b")
    assert safe_num_ctx(entry, _hw_with_vram(6.0), _embed_reserve_gb()) == NUM_CTX_LADDER[0]


def test_safe_num_ctx_never_exceeds_max_num_ctx() -> None:
    """Invariant across the whole local catalog: result ≤ max_num_ctx and on the ladder."""
    hw = _hw_with_vram(96.0)
    reserve = _embed_reserve_gb()
    for entry in MODEL_CATALOG:
        if entry.provider != "ollama":
            continue
        result = safe_num_ctx(entry, hw, reserve)
        max_ctx = entry.max_num_ctx if entry.max_num_ctx is not None else entry.context_tokens
        assert result <= max_ctx, entry.id
        assert result in NUM_CTX_LADDER, entry.id


# ---------------------------------------------------------------------------
# fits_with_embed_reserve fallback arithmetic (FX9.3)
# ---------------------------------------------------------------------------


def test_fits_with_embed_reserve_uses_fallback_weights_and_kv() -> None:
    """When min_vram_gb_at_default_ctx and kv_cache_bytes_per_token are both None,
    the helper falls back to entry.vram_gb for the base residency and the module
    default KV rate for tokens beyond the default ctx — pinned at num_ctx=16384
    so the marginal-KV term is non-zero.

    Synthetic entry: vram_gb=4.0 (the base fallback), context_tokens=16384 so
    the resolved default ctx is min(8192, 16384) = 8192, embed reserve = 2.0.
        required(16384) = 4.0 + (16384-8192) * _DEFAULT_KV_CACHE_BYTES_PER_TOKEN / 1e9 + 2.0
                        = 4.0 + 8192 * 1024 / 1e9 + 2.0
                        ≈ 6.0084 GB
    8 GB budget = 0.80 * 8 = 6.4 → fits (True); 6 GB budget = 4.8 → no (False).
    """
    assert _DEFAULT_KV_CACHE_BYTES_PER_TOKEN == 1024  # guards the arithmetic below
    entry = ModelCatalogEntry(
        id="test/fallback",
        name="Fallback Test Model",
        provider="ollama",
        ollama_tag="test-fallback",
        roles=("smart",),
        vram_gb=4.0,
        disk_gb=3.0,
        context_tokens=16384,
        license="MIT",
        tier=1,
        description="synthetic — exercises the None fallbacks",
        notes="",
        last_reviewed="2026-06-12",
        # min_vram_gb_at_default_ctx and kv_cache_bytes_per_token left None.
    )
    reserve = 2.0
    expected = 4.0 + 8192 * _DEFAULT_KV_CACHE_BYTES_PER_TOKEN / 1e9 + reserve
    assert expected == pytest.approx(6.008_388_608)

    # vram=8.0 → budget 6.4 ≥ 6.0084 → fits.
    assert fits_with_embed_reserve(entry, _hw_with_vram(8.0), reserve, num_ctx=16384) is True
    # vram=6.0 → budget 4.8 < 6.0084 → does not fit.
    assert fits_with_embed_reserve(entry, _hw_with_vram(6.0), reserve, num_ctx=16384) is False


# ---------------------------------------------------------------------------
# Live provider entries merged through the same status machinery
# ---------------------------------------------------------------------------

_STATUS_KEYS = frozenset(ModelStatusDict.__annotations__)
_UNKNOWN_NOTES = (
    "This provider did not say what this model can do, so JARVIS will not offer it for a role."
)


def _live_entry(
    provider: str,
    model_id: str,
    *,
    prefix: str,
    capability: str = "chat",
) -> ModelCatalogEntry:
    return live_model_entry(
        provider,  # type: ignore[arg-type]
        model_id,
        assignment_id=f"{prefix}{model_id}",
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        capability=capability,  # type: ignore[arg-type]
    )


def test_extra_entry_carries_every_model_status_key_and_is_assignable() -> None:
    """A live entry must reach the picker as a complete status entry, not a partial dict."""
    entry = _live_entry("openrouter", "vendor/model-x", prefix="openrouter/")

    statuses = build_model_statuses(
        installed=[],
        current={"smart_model": "openrouter/vendor/model-x"},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=0),
        cloud_api_keys={"openrouter": True},
        extra_entries=(entry,),
    )
    item = next(i for i in statuses if i["id"] == "openrouter/vendor/model-x")

    assert len(_STATUS_KEYS) == 29
    assert set(item) == _STATUS_KEYS
    assert item["name"] == "vendor/model-x"
    assert item["can_assign"] is True
    assert item["status"] == "cloud_active"


def test_display_only_extra_entry_surfaces_its_own_notes_as_the_blocker() -> None:
    entry = _live_entry("openai", "sora-2", prefix="openai/", capability="unknown")

    statuses = build_model_statuses(
        installed=[],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=0),
        cloud_api_keys={"openai": True},
        extra_entries=(entry,),
    )
    item = next(i for i in statuses if i["id"] == "openai/sora-2")

    assert item["can_assign"] is False
    assert item["assign_blocker"] == _UNKNOWN_NOTES


def test_extra_entries_reach_role_recommendations() -> None:
    """Catalog and recommendations cannot disagree about which models exist."""
    entry = _live_entry("openrouter", "vendor/model-x", prefix="openrouter/")

    recommendations = recommendations_for_role(
        "smart",
        installed=[],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=0),
        cloud_api_keys={"openrouter": True},
        extra_entries=(entry,),
    )

    assert any(item["id"] == "openrouter/vendor/model-x" for item in recommendations)


def test_blocker_names_the_provider_display_name_not_its_registry_id() -> None:
    entry = _live_entry("custom_openai_compatible", "org/model-y", prefix="custom_openai/")

    statuses = build_model_statuses(
        installed=[],
        current={},
        embedding_model_name="qwen3-embedding:0.6b",
        hardware=_hardware(tier=0),
        cloud_api_keys={},
        extra_entries=(entry,),
    )
    blocker = next(i for i in statuses if i["id"] == "custom_openai/org/model-y")["assign_blocker"]

    assert blocker == (
        "Configure the Custom OpenAI-compatible endpoint API key before assigning this model."
    )
    assert "custom_openai_compatible" not in str(blocker)
