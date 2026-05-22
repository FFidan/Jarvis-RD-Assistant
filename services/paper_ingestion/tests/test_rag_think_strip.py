import pytest
from paper_ingestion.routers.rag import _strip_think_blocks


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<think>reasoning</think>The answer is 42.", "The answer is 42."),
        ("<think>multi\nline\nthought</think>Final.", "Final."),
        ("No think block at all.", "No think block at all."),
        ("<think>a</think>middle<think>b</think>end", "middleend"),
        ("", ""),
        # Unclosed <think> cases (Qwen3 truncated mid-CoT by max_tokens cap)
        ("<think>truncated reasoning never completes", ""),
        ("The answer is 42.<think>now let me explain why", "The answer is 42."),
        ("<think>first</think>visible<think>unclosed", "visible"),
    ],
)
def test_strip_think_blocks(raw, expected):
    assert _strip_think_blocks(raw) == expected
