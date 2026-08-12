"""Invariant: the CPU image's vision stack resolves from the CPU wheel index.

torch and torchvision ship compiled operator registries that must come from the
same build. When torch resolves from the PyTorch CPU index and torchvision from
PyPI, the pair installs cleanly, passes every import-only check that does not
touch the vision path, and then fails at `import torchvision` with
"operator torchvision::nms does not exist" the first time a PDF is converted.
"""

import re
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
CPU_CONSTRAINTS = ROOT / "services" / "paper_ingestion" / "constraints-cpu.txt"
CUDA_CONSTRAINTS = ROOT / "services" / "paper_ingestion" / "constraints.txt"
LOCK = ROOT / "uv.lock"

# The CPU index serves macOS under the plain version, so the CPU flavor splits
# torch on this marker. Only the non-darwin side ships in the Linux images.
DARWIN_ONLY_MARKER = "sys_platform == 'darwin'"

# Entries start at column 0 as `name==version[ ; marker][ \]` and continue with
# indented `--hash=` lines until the next column-0 line. The generated files
# contain no blank lines, so a blank line cannot be used as a separator.
_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)(?:\s*;\s*(?P<marker>[^\\\n]*?))?\s*\\?$"
)
_HASH = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")
_LOCK_WHEEL = re.compile(
    r"\{ url = \"(?P<url>[^\"]+)\", hash = \"sha256:(?P<digest>[0-9a-f]{64})\""
)


class _Pin(NamedTuple):
    """One pinned distribution entry and the wheel hashes it accepts."""

    version: str
    marker: str
    hashes: frozenset[str]


def _parse_pins(constraints: Path, distribution: str) -> list[_Pin]:
    """Return every pin recorded for *distribution*, in file order."""
    pins: list[_Pin] = []
    collecting: tuple[str, str] | None = None
    hashes: set[str] = set()

    for line in constraints.read_text(encoding="utf-8").splitlines():
        if line[:1].isspace():
            if collecting is not None:
                hashes.update(match["digest"] for match in _HASH.finditer(line))
            continue
        if collecting is not None:
            pins.append(_Pin(*collecting, frozenset(hashes)))
            collecting, hashes = None, set()
        entry = _ENTRY.match(line)
        if entry is not None and entry["name"] == distribution:
            collecting = (entry["version"], entry["marker"] or "")
    if collecting is not None:
        pins.append(_Pin(*collecting, frozenset(hashes)))
    return pins


def _linux_pin(constraints: Path, distribution: str) -> _Pin:
    """Return the pin that governs the Linux images, ignoring any macOS split."""
    pins = [
        pin for pin in _parse_pins(constraints, distribution) if pin.marker != DARWIN_ONLY_MARKER
    ]
    assert len(pins) == 1, (
        f"expected one non-darwin {distribution} pin in {constraints.name}, got {pins}"
    )
    return pins[0]


def _cpu_index_aarch64_hashes(distribution: str) -> frozenset[str]:
    """Return hashes of *distribution* aarch64 wheels served by the CPU index.

    The constraints files record hashes without URLs, so the platform a wheel
    targets is only recoverable from the lock.
    """
    return frozenset(
        wheel["digest"]
        for wheel in _LOCK_WHEEL.finditer(LOCK.read_text(encoding="utf-8"))
        if "/whl/cpu/" in wheel["url"]
        and wheel["url"].rsplit("/", 1)[-1].startswith(f"{distribution}-")
        and "aarch64" in wheel["url"]
    )


def test_parser_finds_the_known_cpu_torch_split() -> None:
    """Guard the parser itself: a vacuous parse would make every other test pass."""
    pins = _parse_pins(CPU_CONSTRAINTS, "torch")

    assert [pin.marker for pin in pins] == [DARWIN_ONLY_MARKER, "sys_platform != 'darwin'"]
    assert pins[1].version == "2.13.0+cpu"
    # Bounded blocks: the whole file holds far more hashes than one entry does.
    assert 0 < len(pins[1].hashes) < 100


def test_cpu_constraints_pin_the_cpu_build_of_torchvision() -> None:
    assert _linux_pin(CPU_CONSTRAINTS, "torchvision").version.endswith("+cpu")


def test_cpu_torchvision_is_not_the_cuda_wheel_set() -> None:
    """The two flavors must not share a wheel: a shared hash means one index served both."""
    cpu = _linux_pin(CPU_CONSTRAINTS, "torchvision")
    cuda = _linux_pin(CUDA_CONSTRAINTS, "torchvision")

    assert cpu.hashes and cuda.hashes
    assert not (cpu.hashes & cuda.hashes)


def test_cpu_torchvision_keeps_an_arm64_wheel() -> None:
    """The CPU image also builds for linux/arm64, so aarch64 must stay resolvable."""
    aarch64 = _cpu_index_aarch64_hashes("torchvision")

    assert aarch64, "the CPU index publishes no aarch64 torchvision wheel"
    assert aarch64 & _linux_pin(CPU_CONSTRAINTS, "torchvision").hashes
