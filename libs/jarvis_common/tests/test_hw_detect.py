import pytest
from jarvis_common.hw_detect import classify_tier


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
