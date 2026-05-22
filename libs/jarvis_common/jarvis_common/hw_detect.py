"""Hardware tier classification — mirrors setup.sh's detect_hw_tier."""

from __future__ import annotations

import subprocess
from typing import Literal

Tier = Literal["cpu", "lt-8", "8-16", "16-24", "24-48", "ge-48"]


def classify_tier(vram_mb: int | None) -> Tier:
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
    return classify_tier(probe_vram_mb())
