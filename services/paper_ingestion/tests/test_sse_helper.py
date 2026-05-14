from jarvis_common.sse import SSE_DONE, sse_event


def test_sse_event_basic():
    assert sse_event({"k": "v"}) == 'data: {"k": "v"}\n\n'


def test_sse_done_constant():
    assert SSE_DONE == "data: [DONE]\n\n"
