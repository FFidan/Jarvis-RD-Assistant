"""Per-VRAM default-model recommendation logic.

This module is ADVISORY only.  It returns a data-driven recommendation of
LiteLLM alias → model assignments for the detected GPU VRAM bucket.  It never
mutates config files, env vars, or the database — callers decide how to surface
the result (e.g. in the system/models API response or setup.sh --check output).

Rationale for each threshold (all values in MiB):
  - VRAM_MIN_MB  (4 096 MiB =  4 GB): minimum to run any useful local LLM;
    below this, recommend CPU-safe conservative stubs so the stack still starts.
  - VRAM_TIER2_MB (10 240 MiB = 10 GB): mid-range GPU (e.g. RTX 3080 / 4070).
    qwen3:8b + qwen3:4b + qwen3-embedding:4b measured to fit a 16 GB card when
    the embedder is kept resident; they also fit a 10–16 GB card without the
    keep-alive pressure.
  - VRAM_TIER3_MB (20 480 MiB = 20 GB): 20–40 GB cards (e.g. A10, RTX 3090).
    Headroom for a 14B smart model while keeping qwen3:4b fast + embedder.
  - VRAM_TIER4_MB (40 960 MiB = 40 GB): large workstation / server GPU
    (e.g. ≥40 GB / 48 GB-class GPU, A40 48 GB).  Recommend qwen3:30b-a3b —
    validated on a 48 GB deployment at 16k context (v0.7).

The ≈16 GB mid-tier defaults (qwen3:8b / qwen3:4b / qwen3-embedding:4b) are the
active litellm/config.yaml entries and the OLLAMA_MODELS bootstrap default as
of 2026-05-18 and are therefore the reference point for tier-2 recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal

# ---------------------------------------------------------------------------
# Named VRAM thresholds (MiB).  Comment each with hardware examples and the
# reasoning behind the cutoff so future maintainers never see a mystery number.
# ---------------------------------------------------------------------------

# Minimum useful VRAM for local inference.  Below this we return a safe CPU
# fallback (qwen3:4b) — it will be slow, but the stack remains bootable.
VRAM_MIN_MB: int = 4_096  # 4 GB

# Entry point for mid-range GPU inference: RTX 3080 10 GB / RTX 4070 12 GB.
# qwen3:8b (~5.5 GB) + qwen3-embedding:4b (~2.4 GB) = ~8 GB, leaving ~2 GB
# headroom on a 10 GB card.  This is the lower-bound of the 16 GB dev-box tier.
VRAM_TIER2_MB: int = 10_240  # 10 GB

# Mid-high: RTX 3090 24 GB / A10 24 GB.  qwen3:14b (~9 GB) fits alongside the
# embedder.  qwen3:4b remains fast; embedder unchanged.
VRAM_TIER3_MB: int = 20_480  # 20 GB

# Large workstation: ≥40 GB / 48 GB-class GPU (A40 48 GB, H100 80 GB, etc.).
# qwen3:30b-a3b + qwen3:4b + embedder all fit with ample headroom.
# Validated on a 48 GB deployment at 16k context (v0.7).
VRAM_TIER4_MB: int = 40_960  # 40 GB


class VramBucket(IntEnum):
    """Discrete hardware tier labels derived from detected VRAM.

    Values deliberately match the tier integers used by model_lifecycle.
    hardware_tier() so they can be compared directly.
    """

    CPU_ONLY = 0  # < VRAM_MIN_MB (4 GB) — no usable GPU / probe failure
    ENTRY = 1  # 4–9 GB — too small for the current default stack
    MID = 2  # 10–19 GB — 16 GB dev box (current defaults)
    MID_HIGH = 3  # 20–39 GB — RTX 3090 / A10 class
    HIGH = 4  # ≥ 40 GB  — 48 GB-class GPU / A40 / H100 class


@dataclass(frozen=True)
class AliasRecommendation:
    """Recommended model for one LiteLLM alias."""

    alias: Literal["smart", "fast", "embed"]
    # Bare Ollama tag (without the "ollama/" LiteLLM prefix), e.g. "qwen3:8b".
    model: str
    # True when this recommendation has not been validated on a real target
    # with a live inference bench.  Frontend/operators should treat it as a
    # best-effort suggestion.
    confirm_on_target: bool = False
    notes: str = ""


@dataclass
class HardwareRecommendation:
    """Advisory recommendation set for the detected VRAM bucket."""

    vram_mb: int | None
    bucket: VramBucket
    # One entry per alias (smart / fast / embed).  Empty list when vram_mb
    # is None (GPU probe failed entirely).
    aliases: list[AliasRecommendation] = field(default_factory=list)
    # Human-readable context string safe to print in setup.sh --check output.
    summary: str = ""


# Validation note reused for the ≥40 GB tier recommendation (AliasRecommendation
# notes + bucket summary both cite the same validated deployment).
_HIGH_TIER_VALIDATED_NOTE: str = (
    "≥40 GB GPU (A40 48 GB or larger); validated on a 48 GB deployment at 16k context (v0.7)"
)

# ---------------------------------------------------------------------------
# Bucket table: maps each VramBucket to its recommended alias assignments.
# Keys are VramBucket members; values are lists of AliasRecommendation.
# ---------------------------------------------------------------------------

_BUCKET_TABLE: dict[VramBucket, list[AliasRecommendation]] = {
    # CPU-only / VRAM probe failure — conservative single-model fallback.
    # qwen3:4b runs on CPU but will be slow; embedder stays as-is.
    VramBucket.CPU_ONLY: [
        AliasRecommendation(
            alias="smart", model="qwen3:4b", notes="CPU fallback — GPU not detected"
        ),
        AliasRecommendation(
            alias="fast", model="qwen3:4b", notes="CPU fallback — GPU not detected"
        ),
        AliasRecommendation(
            alias="embed", model="qwen3-embedding:4b", notes="CPU fallback — GPU not detected"
        ),
    ],
    # Entry-class GPU (4–9 GB): too small for qwen3:8b alongside the embedder.
    # Use qwen3:4b for both smart and fast; keep the same embedder.
    VramBucket.ENTRY: [
        AliasRecommendation(
            alias="smart",
            model="qwen3:4b",
            notes="4–9 GB GPU — insufficient for 8b + embedder concurrently",
        ),
        AliasRecommendation(alias="fast", model="qwen3:4b"),
        AliasRecommendation(alias="embed", model="qwen3-embedding:4b"),
    ],
    # Mid-tier (10–19 GB): the current 16 GB dev-box defaults, measured to fit.
    # qwen3:8b (~5.5 GB) + qwen3-embedding:4b (~2.4 GB) leaves ~2–8 GB spare.
    VramBucket.MID: [
        AliasRecommendation(
            alias="smart",
            model="qwen3:8b",
            notes="Default for 10–19 GB GPU; measured on ≈16 GB GPU",
        ),
        AliasRecommendation(alias="fast", model="qwen3:4b"),
        AliasRecommendation(alias="embed", model="qwen3-embedding:4b"),
    ],
    # Mid-high (20–39 GB): room for a 14B model alongside embedder.
    VramBucket.MID_HIGH: [
        AliasRecommendation(
            alias="smart",
            model="qwen3:14b",
            notes="20–39 GB GPU; qwen3:14b (~9 GB) + embedder fits A10/3090",
        ),
        AliasRecommendation(alias="fast", model="qwen3:4b"),
        AliasRecommendation(alias="embed", model="qwen3-embedding:4b"),
    ],
    # High (≥ 40 GB): workstation class.  qwen3:30b-a3b validated on a
    # 48 GB deployment at 16k context (v0.7).
    VramBucket.HIGH: [
        AliasRecommendation(
            alias="smart",
            model="qwen3:30b-a3b",
            notes=_HIGH_TIER_VALIDATED_NOTE,
        ),
        AliasRecommendation(alias="fast", model="qwen3:4b"),
        AliasRecommendation(alias="embed", model="qwen3-embedding:4b"),
    ],
}

# Human-readable summary strings for each bucket.
_BUCKET_SUMMARIES: dict[VramBucket, str] = {
    VramBucket.CPU_ONLY: (
        "No GPU detected — Ollama will run on CPU. "
        "Consider a GPU with ≥10 GB VRAM for comfortable inference speed."
    ),
    VramBucket.ENTRY: (
        "Entry-class GPU (<10 GB) detected. "
        "Recommend qwen3:4b for smart+fast (qwen3:8b + embedder may not fit concurrently)."
    ),
    VramBucket.MID: (
        "Mid-tier GPU (10–19 GB) detected. "
        "Default stack (qwen3:8b / qwen3:4b / qwen3-embedding:4b) fits with headroom."
    ),
    VramBucket.MID_HIGH: (
        "Mid-high GPU (20–39 GB) detected. "
        "Upgrade smart to qwen3:14b for better quality with ample headroom."
    ),
    VramBucket.HIGH: (
        "High-end GPU (≥40 GB) detected. "
        "Recommend qwen3:30b-a3b for smart (validated on a 48 GB deployment "
        "at 16k context, v0.7)."
    ),
}


def _classify(vram_mb: int) -> VramBucket:
    """Map a raw VRAM MiB value to a VramBucket.

    Uses the named VRAM_* thresholds — no magic numbers.
    """
    if vram_mb < VRAM_MIN_MB:
        return VramBucket.CPU_ONLY
    if vram_mb < VRAM_TIER2_MB:
        return VramBucket.ENTRY
    if vram_mb < VRAM_TIER3_MB:
        return VramBucket.MID
    if vram_mb < VRAM_TIER4_MB:
        return VramBucket.MID_HIGH
    return VramBucket.HIGH


def recommend_models(vram_mb: int | None) -> HardwareRecommendation:
    """Return an advisory model recommendation for the given VRAM amount.

    Parameters
    ----------
    vram_mb:
        Total GPU VRAM in mebibytes (MiB) as returned by nvidia-smi
        ``--format=csv,noheader,nounits`` (which reports in MiB).  Pass
        ``None`` when the GPU probe failed entirely — a safe CPU-fallback
        recommendation is returned with an empty aliases list so callers
        can distinguish "no GPU" from "tiny GPU".

    Returns
    -------
    HardwareRecommendation
        Advisory recommendation.  Never raises.  ``confirm_on_target`` on
        individual aliases signals that a live bench is still outstanding.

    """
    if vram_mb is None:
        return HardwareRecommendation(
            vram_mb=None,
            bucket=VramBucket.CPU_ONLY,
            aliases=[],
            summary=(
                "GPU probe failed — could not determine VRAM. "
                "Using conservative defaults (qwen3:4b)."
            ),
        )

    bucket = _classify(vram_mb)
    return HardwareRecommendation(
        vram_mb=vram_mb,
        bucket=bucket,
        aliases=list(_BUCKET_TABLE[bucket]),
        summary=_BUCKET_SUMMARIES[bucket],
    )
