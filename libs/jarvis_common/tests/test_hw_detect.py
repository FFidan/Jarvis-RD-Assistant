import pytest
from jarvis_common.hw_detect import classify_tier, detect_vendor, vendor_from_env


@pytest.mark.parametrize(
    "mb, expected",
    [
        (None, "cpu"),
        (0, "cpu"),
        (4096, "lt-8"),
        (12288, "8-16"),
        (22528, "16-24"),
        (36864, "24-48"),
        (49152, "ge-48"),
        (98304, "ge-48"),
    ],
)
def test_classify_tier(mb, expected):
    assert classify_tier(mb) == expected


@pytest.mark.parametrize("vendor", ["nvidia", "amd", "intel", "none"])
def test_detect_vendor_env_wins(monkeypatch, vendor):
    monkeypatch.setenv("JARVIS_GPU_VENDOR", vendor)
    assert detect_vendor() == vendor


def test_detect_vendor_invalid_env_falls_back_to_probe(monkeypatch):
    monkeypatch.setenv("JARVIS_GPU_VENDOR", "matrox")
    monkeypatch.setattr("jarvis_common.hw_detect.probe_vram_mb", lambda: 8192)
    assert detect_vendor() == "nvidia"


def test_detect_vendor_no_env_no_gpu(monkeypatch):
    monkeypatch.delenv("JARVIS_GPU_VENDOR", raising=False)
    monkeypatch.setattr("jarvis_common.hw_detect.probe_vram_mb", lambda: None)
    assert detect_vendor() == "none"


@pytest.mark.parametrize(
    "raw, expected",
    [("amd", "amd"), (" NVIDIA ", "nvidia"), ("", None), ("matrox", None)],
)
def test_vendor_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("JARVIS_GPU_VENDOR", raw)
    assert vendor_from_env() == expected
