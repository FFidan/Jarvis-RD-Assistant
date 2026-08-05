"""Hardware tier classification — mirrors setup.sh's detect_hw_tier."""

from __future__ import annotations

import os
import subprocess
from typing import Literal, cast

Tier = Literal["cpu", "lt-8", "8-16", "16-24", "24-48", "ge-48"]
Vendor = Literal["nvidia", "amd", "intel", "none"]

_VENDORS: tuple[Vendor, ...] = ("nvidia", "amd", "intel", "none")


def classify_tier(vram_mb: int | None) -> Tier:
    """Map raw VRAM megabytes to a named hardware tier used for model-routing decisions.

    These cutoffs (8/16/24/48 GB) intentionally differ from
    :mod:`jarvis_common.hardware_fit`'s model-recommendation thresholds
    (4/10/20/40 GB): this function mirrors the shell contract in setup.sh's
    detect_hw_tier, while hardware_fit's thresholds are tuned independently
    around specific model memory footprints. Do not unify them.
    """
    if vram_mb is None or vram_mb <= 0:
        return "cpu"
    if vram_mb < 8000:
        return "lt-8"
    if vram_mb < 16000:
        return "8-16"
    if vram_mb < 24000:
        return "16-24"
    if vram_mb < 48000:
        return "24-48"
    return "ge-48"


def probe_vram_mb() -> int | None:
    """Run nvidia-smi and return the largest GPU's total VRAM in MB, or None."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    values = []
    for line in out.stdout.splitlines():
        s = line.strip()
        if s.isdigit():
            values.append(int(s))
    return max(values) if values else None


def detect_tier() -> Tier:
    """Convenience wrapper: probe VRAM then classify into a tier."""
    return classify_tier(probe_vram_mb())


def vendor_from_env() -> Vendor | None:
    """Parse JARVIS_GPU_VENDOR (written into .env by setup.sh).

    Returns None when the variable is unset or not a recognized vendor, so
    callers can distinguish an explicit host-side answer from inference.
    """
    value = os.environ.get("JARVIS_GPU_VENDOR", "").strip().lower()
    return cast(Vendor, value) if value in _VENDORS else None


def detect_vendor() -> Vendor:
    """GPU vendor: the setup-written host value wins, else in-container probe.

    nvidia-smi is the only vendor tool available inside the service images, so
    without the env conduit an AMD/Intel host is indistinguishable from CPU.
    """
    env_vendor = vendor_from_env()
    if env_vendor is not None:
        return env_vendor
    return "nvidia" if probe_vram_mb() is not None else "none"
