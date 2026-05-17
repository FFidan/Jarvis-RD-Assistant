"""Unit tests for jarvis_common.hardware_fit — per-VRAM recommendation logic.

Coverage:
- 16 GB dev-box (16 384 MiB → MID bucket) → qwen3:8b / qwen3:4b / qwen3-embedding:4b
- 48 GB workstation (49 152 MiB → HIGH bucket) → qwen3:32b (confirm_on_target=True)
- Entry-class GPU (8 192 MiB → ENTRY bucket) → qwen3:4b for smart+fast
- CPU-only / below-min (2 048 MiB) → CPU_ONLY bucket, conservative fallback
- Exact threshold boundaries (VRAM_TIER2_MB, VRAM_TIER4_MB)
- None VRAM → safe default, no crash, empty aliases list
- All buckets covered, no mystery numbers
"""

from __future__ import annotations

from jarvis_common.hardware_fit import (
    VRAM_MIN_MB,
    VRAM_TIER2_MB,
    VRAM_TIER3_MB,
    VRAM_TIER4_MB,
    HardwareRecommendation,
    VramBucket,
    recommend_models,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alias(rec: HardwareRecommendation, alias: str) -> str:
    """Return the recommended model string for a given alias, or raise if absent."""
    for a in rec.aliases:
        if a.alias == alias:
            return a.model
    raise KeyError(f"alias {alias!r} not found in recommendation {rec!r}")


def _confirm(rec: HardwareRecommendation, alias: str) -> bool:
    """Return confirm_on_target for a given alias."""
    for a in rec.aliases:
        if a.alias == alias:
            return a.confirm_on_target
    raise KeyError(f"alias {alias!r} not found in recommendation {rec!r}")


# ---------------------------------------------------------------------------
# 16 GB dev-box path (active litellm/config.yaml defaults)
# ---------------------------------------------------------------------------


def test_16gb_bucket_is_mid():
    """16 GB (16 384 MiB) classifies as VramBucket.MID."""
    rec = recommend_models(16_384)
    assert rec.bucket == VramBucket.MID


def test_16gb_smart_is_qwen3_8b():
    """16 GB → smart alias = qwen3:8b (the active config.yaml default)."""
    rec = recommend_models(16_384)
    assert _alias(rec, "smart") == "qwen3:8b"


def test_16gb_fast_is_qwen3_4b():
    """16 GB → fast alias = qwen3:4b (the active config.yaml default)."""
    rec = recommend_models(16_384)
    assert _alias(rec, "fast") == "qwen3:4b"


def test_16gb_embed_is_qwen3_embedding_4b():
    """16 GB → embed alias = qwen3-embedding:4b (the active config.yaml default)."""
    rec = recommend_models(16_384)
    assert _alias(rec, "embed") == "qwen3-embedding:4b"


def test_16gb_confirm_on_target_false():
    """16 GB recommendations are all measured — confirm_on_target must be False."""
    rec = recommend_models(16_384)
    assert _confirm(rec, "smart") is False
    assert _confirm(rec, "fast") is False
    assert _confirm(rec, "embed") is False


def test_16gb_summary_non_empty():
    """16 GB bucket has a human-readable summary."""
    rec = recommend_models(16_384)
    assert rec.summary


# ---------------------------------------------------------------------------
# 48 GB workstation path (RTX 5880 Ada target — confirm_on_target)
# ---------------------------------------------------------------------------


def test_48gb_bucket_is_high():
    """48 GB (49 152 MiB) classifies as VramBucket.HIGH."""
    rec = recommend_models(49_152)
    assert rec.bucket == VramBucket.HIGH


def test_48gb_smart_is_qwen3_32b():
    """48 GB → smart alias = qwen3:32b (larger model for high-VRAM box)."""
    rec = recommend_models(49_152)
    assert _alias(rec, "smart") == "qwen3:32b"


def test_48gb_smart_confirm_on_target_true():
    """48 GB smart recommendation has confirm_on_target=True (bench not yet run)."""
    rec = recommend_models(49_152)
    assert _confirm(rec, "smart") is True


def test_48gb_fast_confirm_on_target_false():
    """48 GB fast/embed are measured defaults — confirm_on_target must be False."""
    rec = recommend_models(49_152)
    assert _confirm(rec, "fast") is False
    assert _confirm(rec, "embed") is False


def test_48gb_vram_mb_preserved():
    """vram_mb is echoed back in the recommendation."""
    rec = recommend_models(49_152)
    assert rec.vram_mb == 49_152


# ---------------------------------------------------------------------------
# Below-minimum VRAM paths (CPU fallback / entry-class)
# ---------------------------------------------------------------------------


def test_below_min_vram_is_cpu_only():
    """VRAM below VRAM_MIN_MB → VramBucket.CPU_ONLY."""
    rec = recommend_models(VRAM_MIN_MB - 1)
    assert rec.bucket == VramBucket.CPU_ONLY


def test_zero_vram_cpu_only():
    """0 MiB VRAM → CPU_ONLY bucket, no crash."""
    rec = recommend_models(0)
    assert rec.bucket == VramBucket.CPU_ONLY


def test_entry_class_gpu_8gb():
    """Entry-class GPU (8 192 MiB = 8 GB) → VramBucket.ENTRY."""
    rec = recommend_models(8_192)
    assert rec.bucket == VramBucket.ENTRY


def test_entry_class_smart_is_qwen3_4b():
    """Entry-class GPU: smart = qwen3:4b (8b + embedder may not fit)."""
    rec = recommend_models(8_192)
    assert _alias(rec, "smart") == "qwen3:4b"


# ---------------------------------------------------------------------------
# Exact threshold boundary behaviour
# ---------------------------------------------------------------------------


def test_tier2_lower_boundary_is_mid():
    """Exactly VRAM_TIER2_MB (10 240 MiB) → VramBucket.MID, not ENTRY."""
    rec = recommend_models(VRAM_TIER2_MB)
    assert rec.bucket == VramBucket.MID


def test_just_below_tier2_is_entry():
    """One MiB below VRAM_TIER2_MB → VramBucket.ENTRY."""
    rec = recommend_models(VRAM_TIER2_MB - 1)
    assert rec.bucket == VramBucket.ENTRY


def test_tier3_lower_boundary_is_mid_high():
    """Exactly VRAM_TIER3_MB (20 480 MiB) → VramBucket.MID_HIGH."""
    rec = recommend_models(VRAM_TIER3_MB)
    assert rec.bucket == VramBucket.MID_HIGH


def test_tier4_lower_boundary_is_high():
    """Exactly VRAM_TIER4_MB (40 960 MiB) → VramBucket.HIGH."""
    rec = recommend_models(VRAM_TIER4_MB)
    assert rec.bucket == VramBucket.HIGH


def test_just_below_tier4_is_mid_high():
    """One MiB below VRAM_TIER4_MB → VramBucket.MID_HIGH, not HIGH."""
    rec = recommend_models(VRAM_TIER4_MB - 1)
    assert rec.bucket == VramBucket.MID_HIGH


# ---------------------------------------------------------------------------
# None VRAM — probe failure path
# ---------------------------------------------------------------------------


def test_none_vram_no_crash():
    """recommend_models(None) must not raise."""
    rec = recommend_models(None)
    assert rec is not None


def test_none_vram_bucket_is_cpu_only():
    """None VRAM → CPU_ONLY bucket."""
    rec = recommend_models(None)
    assert rec.bucket == VramBucket.CPU_ONLY


def test_none_vram_empty_aliases():
    """None VRAM → empty aliases list (distinguishes 'no GPU' from 'tiny GPU')."""
    rec = recommend_models(None)
    assert rec.aliases == []


def test_none_vram_summary_non_empty():
    """None VRAM → summary string describes the probe failure."""
    rec = recommend_models(None)
    assert rec.summary


def test_none_vram_vram_mb_is_none():
    """None input is echoed back in vram_mb field."""
    rec = recommend_models(None)
    assert rec.vram_mb is None


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------


def test_returns_hardware_recommendation_instance():
    """recommend_models always returns a HardwareRecommendation."""
    assert isinstance(recommend_models(16_384), HardwareRecommendation)
    assert isinstance(recommend_models(0), HardwareRecommendation)
    assert isinstance(recommend_models(None), HardwareRecommendation)


def test_all_non_none_buckets_have_three_aliases():
    """Every non-None VRAM value produces exactly 3 aliases (smart/fast/embed)."""
    for vram in (0, 2_048, 8_192, 16_384, 32_768, 49_152):
        rec = recommend_models(vram)
        assert len(rec.aliases) == 3, f"Expected 3 aliases for vram={vram}: {rec.aliases}"
        aliases_returned = {a.alias for a in rec.aliases}
        assert aliases_returned == {"smart", "fast", "embed"}, (
            f"Unexpected aliases for vram={vram}: {aliases_returned}"
        )


def test_named_thresholds_are_consistent():
    """Named threshold constants form an ascending sequence — no misorderings."""
    assert VRAM_MIN_MB < VRAM_TIER2_MB < VRAM_TIER3_MB < VRAM_TIER4_MB


def test_threshold_values_match_documented_gb():
    """Threshold values match the documented GB values in module docstring."""
    assert VRAM_MIN_MB == 4_096  # 4 GB
    assert VRAM_TIER2_MB == 10_240  # 10 GB
    assert VRAM_TIER3_MB == 20_480  # 20 GB
    assert VRAM_TIER4_MB == 40_960  # 40 GB
