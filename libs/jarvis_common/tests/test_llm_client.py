"""Direct tests for the shared LiteLLM chat helper."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common import llm_client
from jarvis_common.llm_client import strip_think_streaming


def test_get_litellm_config_reads_base_url_from_env(monkeypatch):
    """get_litellm_config should resolve LITELLM_BASE_URL from environment."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    config = llm_client.get_litellm_config()

    assert config == llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")


def test_get_litellm_config_uses_default_url(monkeypatch):
    """get_litellm_config should fall back to DEFAULT_LITELLM_BASE_URL."""
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)

    config = llm_client.get_litellm_config()

    assert config.base_url == llm_client.DEFAULT_LITELLM_BASE_URL


def test_build_litellm_headers_returns_empty_when_key_unset(monkeypatch):
    """build_litellm_headers returns {} when LITELLM_MASTER_KEY is not set."""
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")

    assert llm_client.build_litellm_headers(config) == {}


def test_observe_disables_implicit_object_capture(monkeypatch):
    """Trace decorators must not serialize clients, pools, or retrieval objects."""
    captured: dict[str, object] = {}

    def fake_observe(*args, **kwargs):
        captured.update(kwargs)

        def decorator(function):
            return function

        return decorator

    monkeypatch.setattr(llm_client, "_langfuse_observe", fake_observe)

    @llm_client.observe(as_type="generation")
    def traced_boundary() -> None:
        return None

    traced_boundary()

    assert captured["capture_input"] is False
    assert captured["capture_output"] is False


def test_observation_values_are_bounded_without_object_repr():
    """Exported content is capped and unsupported objects reveal only their type."""
    bounded = llm_client._bounded_observation_value("x" * 25_000)

    assert len(bounded) == 20_000
    assert bounded.endswith("..._truncated")
    assert llm_client._bounded_observation_value(object()) == "<object>"


def test_embedding_matrix_is_summarized_by_shape_not_by_a_prefix_of_floats():
    """An embedding batch is recorded as its dimensions, not as truncated numbers.

    Serializing the matrix builds the whole JSON string only to keep a 20,000
    character prefix of coordinates, which is both the largest allocation on the
    observed path and useless to read.
    """
    assert llm_client._bounded_observation_value([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]) == (
        "<embedding matrix 2x3>"
    )
    # The chat-message and prompt forms carry meaning and stay verbatim.
    assert llm_client._bounded_observation_value([{"role": "user", "content": "hi"}]) == (
        '[{"role":"user","content":"hi"}]'
    )
    assert llm_client._bounded_observation_value(["first", "second"]) == '["first","second"]'
    assert llm_client._bounded_observation_value([[1, "two"]]) == '[[1,"two"]]'
    assert llm_client._bounded_observation_value([]) == "[]"


def test_build_litellm_headers_returns_bearer_when_key_set(monkeypatch):
    """build_litellm_headers returns Authorization: Bearer <key> when LITELLM_MASTER_KEY is set."""
    monkeypatch.delenv("LITELLM_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-secret-key-abc123")
    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")

    assert llm_client.build_litellm_headers(config) == {
        "Authorization": "Bearer test-secret-key-abc123"
    }


def test_build_litellm_headers_reads_key_from_file_var(monkeypatch, tmp_path):
    """LITELLM_MASTER_KEY_FILE must be honoured ahead of the plain env var.

    When ``LITELLM_MASTER_KEY_FILE`` points to a non-empty file, the resolved
    key (whitespace-stripped) is used in the Bearer header even if the legacy
    ``LITELLM_MASTER_KEY`` env var holds something different.  This is the
    behaviour Docker Secrets relies on.
    """
    secret_file = tmp_path / "litellm_master_key.txt"
    secret_file.write_text("file-bearer-secret-xyz\n")
    monkeypatch.setenv("LITELLM_MASTER_KEY_FILE", str(secret_file))
    # Plain env var should be ignored when _FILE is present.
    monkeypatch.setenv("LITELLM_MASTER_KEY", "this-should-not-win")
    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")

    assert llm_client.build_litellm_headers(config) == {
        "Authorization": "Bearer file-bearer-secret-xyz"
    }


def test_detect_visible_work_notes_flags_process_preambles():
    """Visible-answer hygiene catches leading process prose without storing text."""
    detection = llm_client.detect_visible_work_notes(
        "Let me look at the papers first. The answer is unsupported."
    )
    contraction = llm_client.detect_visible_work_notes(
        "I'll check the sources first. The answer is unsupported."
    )

    assert detection.has_work_notes is True
    assert detection.marker == "let me"
    assert contraction.has_work_notes is True
    assert contraction.marker == "i'll check"


def test_could_be_visible_work_note_prefix_only_holds_risky_starts():
    """Partial-stream quarantine only holds prefixes that could become work notes."""
    assert llm_client.could_be_visible_work_note_prefix("Let") is True
    assert llm_client.could_be_visible_work_note_prefix("I") is True
    assert llm_client.could_be_visible_work_note_prefix("I need t") is True
    assert llm_client.could_be_visible_work_note_prefix("I need to comp") is True
    assert llm_client.could_be_visible_work_note_prefix("First, I n") is True
    assert llm_client.could_be_visible_work_note_prefix("First, I compare") is False
    assert llm_client.could_be_visible_work_note_prefix("I compare") is False
    assert llm_client.could_be_visible_work_note_prefix("Hello") is False


def test_detect_visible_work_notes_flags_pure_leading_preamble():
    """A leading work-note preamble is still stripped even when a real answer follows."""
    detection = llm_client.detect_visible_work_notes(
        "Let me analyze this paper.\n\n"
        "The control experiment in Table 2 shows a significant effect (p=0.01)."
    )

    assert detection.has_work_notes is True
    assert detection.marker == "let me"


def test_detect_visible_work_notes_allows_later_paragraph_work_note_phrasing():
    """A substantive answer is not discarded just because a later paragraph opens
    with work-note phrasing — only a *leading* work note should trigger discard.
    """
    detection = llm_client.detect_visible_work_notes(
        "Paper A reports a significant effect (p=0.01) on the primary endpoint.\n\n"
        "I need to check the second study's sample size before drawing conclusions, "
        "but the primary result holds across both cohorts.\n\n"
        "Overall, the evidence supports the hypothesis."
    )

    assert detection.has_work_notes is False


def test_detect_visible_work_notes_allows_normal_final_answers():
    """Ordinary final-answer language is not classified as work notes."""
    safe_answers = [
        "The problem is addressed by the control experiment in Table 2.",
        "The analysis section reports a smaller effect size than the abstract.",
        "I compare the papers by their reported evaluation settings.",
    ]

    assert all(
        not llm_client.detect_visible_work_notes(answer).has_work_notes for answer in safe_answers
    )


def test_strip_think_blocks_removes_multiple_sections():
    """strip_think_blocks should remove all think blocks before JSON parsing."""
    raw = '<think>draft</think>\n{"step":1}\n<think>hidden</think>\n{"answer":"ok"}'

    cleaned = llm_client.strip_think_blocks(raw)

    assert "<think>" not in cleaned
    assert cleaned == '{"step":1}\n\n{"answer":"ok"}'


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Unclosed <think> at end-of-string (Qwen3 truncated mid-CoT by max_tokens cap)
        ("<think>truncated reasoning never completes", ""),
        # Visible prefix + unclosed <think>
        ("The answer is 42.<think>now let me explain why", "The answer is 42."),
        # Mixed: one closed block then one unclosed block (mid-stream truncation)
        ("<think>first</think>visible<think>unclosed", "visible"),
    ],
)
def test_strip_think_blocks_unclosed_tag_truncation(raw, expected):
    from jarvis_common.llm_client import strip_think_blocks

    assert strip_think_blocks(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "<think>hidden</think>",
        "<think>truncated reasoning",
        "   <think>hidden</think>   ",
    ],
)
def test_strip_think_blocks_only_reasoning_has_no_visible_content(raw: str) -> None:
    assert llm_client.strip_think_blocks(raw) == ""


def test_chat_completion_options_with_response_format_preserves_other_fields():
    """with_response_format should only swap the response format field."""
    options = llm_client.ChatCompletionOptions(
        model="smart",
        max_tokens=77,
        temperature=0.3,
        timeout=45.0,
    )

    updated = options.with_response_format({"type": "json_object"})

    assert updated.response_format == {"type": "json_object"}
    assert updated.model == "smart"
    assert updated.max_tokens == 77
    assert updated.temperature == 0.3
    assert updated.timeout == 45.0


@pytest.mark.asyncio
async def test_request_chat_completion_content_requires_prompt_or_messages():
    """The low-level helper should reject empty invocations before any HTTP call."""
    with pytest.raises(ValueError, match="Either prompt or messages must be provided"):
        await llm_client.request_chat_completion_content(
            AsyncMock(),
            options=llm_client.ChatCompletionOptions(),
        )


@pytest.mark.asyncio
async def test_request_chat_completion_content_forwards_response_format(monkeypatch):
    """The lower-level request helper should pass through response_format unchanged."""
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '<think>hidden</think>\n{"answer":"ok"}'}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response

    result = await llm_client.request_chat_completion_content(
        http_client,
        prompt="Summarize this.",
        options=llm_client.ChatCompletionOptions(
            model="smart",
            max_tokens=77,
            temperature=0.3,
            timeout=45.0,
            response_format={"type": "json_object"},
        ),
        config=llm_client.LiteLLMConfig(base_url="http://litellm.test:4000"),
    )

    assert result == '{"answer":"ok"}'
    http_client.post.assert_awaited_once_with(
        "http://litellm.test:4000/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "Summarize this."}],
            "max_tokens": 77,
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        },
        headers={},
        timeout=45.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["<think>hidden</think>", "   "])
async def test_request_chat_completion_content_rejects_empty_visible_content(raw: str, monkeypatch):
    """The scalar helper must fail explicitly when think stripping leaves no answer."""
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": raw}}]}
    http_client = AsyncMock()
    http_client.post.return_value = response

    with pytest.raises(llm_client.EmptyVisibleLLMContentError, match="no visible content"):
        await llm_client.request_chat_completion_content(
            http_client,
            prompt="Summarize this.",
            options=llm_client.ChatCompletionOptions(model="smart"),
            config=llm_client.LiteLLMConfig(base_url="http://litellm.test:4000"),
        )


@pytest.mark.asyncio
async def test_embed_texts_sorts_embeddings_by_index():
    """embed_texts should restore original input order using LiteLLM indexes."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {"index": 1, "embedding": [2.0]},
            {"index": 0, "embedding": [1.0]},
        ]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response

    result = await llm_client.embed_texts(
        http_client,
        ["first", "second"],
        config=llm_client.LiteLLMConfig(base_url="http://litellm.test:4000"),
    )

    assert result == [[1.0], [2.0]]


@pytest.mark.asyncio
async def test_chat_sink_refuses_quarantine_before_http(monkeypatch, tmp_path):
    from jarvis_common.maintenance import OutboundEgressBlockedError

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    http_client = AsyncMock()

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        await llm_client.request_chat_completion_content(
            http_client,
            prompt="hello",
            options=llm_client.ChatCompletionOptions(),
        )

    http_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_sink_refuses_quarantine_before_http(monkeypatch, tmp_path):
    from jarvis_common.maintenance import OutboundEgressBlockedError

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.write_text("malformed")
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    http_client = AsyncMock()

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        await llm_client.embed_texts(http_client, ["hello"])

    http_client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# call_llm_structured guards (timeout passthrough, model fallback, validation)
# ---------------------------------------------------------------------------


def _make_instructor_recorder(recorded: dict):
    """Return a fake already-patched openai client for call_llm_structured tests.

    call_llm_structured uses the passed client directly (no double-wrapping),
    so tests need a client whose .chat.completions.create() records kwargs.
    """

    async def _create(**kwargs):
        recorded.update(kwargs)
        return _DummyResponse()

    class _DummyResponse:
        pass

    class _Completions:
        create = staticmethod(_create)

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        chat = _Chat()

    return _FakeClient(), _DummyResponse


@pytest.mark.asyncio
async def test_call_llm_structured_passes_timeout_through():
    """call_llm_structured must forward options.timeout to the OpenAI SDK call."""
    from pydantic import BaseModel

    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    class _Out(BaseModel):
        pass

    await llm_client.call_llm_structured(
        fake_client,
        response_model=_Out,
        prompt="hi",
        options=llm_client.ChatCompletionOptions(model="smart", timeout=42.0),
    )

    assert recorded.get("timeout") == 42.0
    assert recorded.get("model") == "smart"


@pytest.mark.asyncio
async def test_call_llm_structured_records_only_explicit_generation_content(monkeypatch):
    """The generation trace receives bounded prompt/output values, never the client."""
    recorded_call: dict = {}
    observations: list[dict[str, object]] = []
    fake_client, _ = _make_instructor_recorder(recorded_call)

    monkeypatch.setattr(
        llm_client,
        "record_generation_observation",
        lambda **values: observations.append(values),
    )

    await llm_client.call_llm_structured(
        fake_client,
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        prompt="bounded prompt",
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert observations[0] == {
        "input_value": [{"role": "user", "content": "bounded prompt"}],
        "model": "smart",
    }
    assert set(observations[1]) == {"output_value"}
    assert fake_client not in observations[0].values()


@pytest.mark.asyncio
async def test_structured_llm_sink_refuses_quarantine_before_sdk_call(monkeypatch, tmp_path):
    from jarvis_common.maintenance import OutboundEgressBlockedError
    from pydantic import BaseModel

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    class _Out(BaseModel):
        pass

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        await llm_client.call_llm_structured(
            fake_client,
            response_model=_Out,
            prompt="hello",
        )

    assert recorded == {}


@pytest.mark.asyncio
async def test_call_llm_structured_raises_when_client_is_none():
    """A clear RuntimeError is preferable to a downstream Instructor crash."""
    with pytest.raises(RuntimeError, match="openai_client is required"):
        await llm_client.call_llm_structured(
            None,  # type: ignore[arg-type]
            response_model=type("_X", (), {}),  # type: ignore[arg-type]
            prompt="hi",
            options=llm_client.ChatCompletionOptions(model="smart"),
        )


@pytest.mark.asyncio
async def test_call_llm_structured_raises_without_prompt_or_messages():
    """The structured helper must reject empty invocations before any LLM call."""
    with pytest.raises(ValueError, match="Either prompt or messages must be provided"):
        await llm_client.call_llm_structured(
            MagicMock(),
            response_model=type("_X", (), {}),  # type: ignore[arg-type]
            options=llm_client.ChatCompletionOptions(model="smart"),
        )


@pytest.mark.asyncio
async def test_call_llm_structured_accepts_prompt_only():
    """A prompt-only call should produce a single user message and proceed."""
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    await llm_client.call_llm_structured(
        fake_client,  # type: ignore[arg-type]
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        prompt="hello",
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert recorded.get("messages") == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_call_llm_structured_accepts_messages_only():
    """A messages-only call should pass them through unchanged."""
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    await llm_client.call_llm_structured(
        fake_client,  # type: ignore[arg-type]
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        messages=[{"role": "system", "content": "be brief"}],
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert recorded.get("messages") == [{"role": "system", "content": "be brief"}]


@pytest.mark.asyncio
async def test_call_llm_structured_appends_prompt_to_messages():
    """Per docstring, prompt is appended as a user message when both are given."""
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    await llm_client.call_llm_structured(
        fake_client,  # type: ignore[arg-type]
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        messages=[{"role": "system", "content": "be brief"}],
        prompt="hi there",
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert recorded.get("messages") == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi there"},
    ]


@pytest.mark.asyncio
async def test_call_llm_structured_rejects_empty_model():
    """Empty model alias must raise ValueError, never silently fall back to a URL."""
    with pytest.raises(ValueError, match="model must be a non-empty"):
        await llm_client.call_llm_structured(
            MagicMock(),
            response_model=type("_X", (), {}),  # type: ignore[arg-type]
            prompt="hi",
            options=llm_client.ChatCompletionOptions(model=""),
        )


# ---------------------------------------------------------------------------
# Observability decorator coverage (D.6)
# ---------------------------------------------------------------------------
#
# Exactly nine functions are
# trace-boundary roots. Each MUST be wrapped by ``@observe()``.  When the
# decorator is removed, ``__wrapped__`` disappears — this test is the
# canary.

_BOUNDARY_FUNCTIONS: list[tuple[str, str]] = [
    # (module path, attribute path within module)
    ("paper_ingestion.pulse.job", "run_pulse"),
    ("paper_ingestion.rag.streaming", "prepare_single_paper_rag"),
    ("paper_ingestion.rag.streaming", "prepare_cross_paper_rag"),
    ("paper_ingestion.extraction.core", "batch_extract"),
    ("paper_ingestion.extraction.core", "extract_fields_for_paper"),
    ("paper_ingestion.extraction.entities", "extract_entities_for_paper"),
    ("learning_engine.card_generator", "CardGenerator.generate_cards"),
    ("paper_ingestion.weekly_summary", "generate_weekly_summary"),
    ("paper_ingestion.services.contradictions", "scan_contradictions"),
]


def _resolve(module_path: str, attr_path: str):
    import importlib  # noqa: PLC0415

    obj = importlib.import_module(module_path)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def test_boundary_functions_are_observed():
    """Every trace-boundary function in the observability contract must carry @observe()."""
    pytest.importorskip("paper_ingestion", reason="paper_ingestion service not installed")
    missing: list[str] = []
    for module_path, attr_path in _BOUNDARY_FUNCTIONS:
        try:
            fn = _resolve(module_path, attr_path)
        except (ImportError, AttributeError) as exc:
            pytest.skip(f"Could not import {module_path}.{attr_path}: {exc}")
        if not hasattr(fn, "__wrapped__"):
            missing.append(f"{module_path}.{attr_path}")
    assert not missing, (
        "Trace-boundary functions missing @observe() (per docs/contracts/04-observability.md §3): "
        + ", ".join(missing)
    )


def test_langfuse_export_rechecks_quarantine_after_initialization(monkeypatch, tmp_path):
    """A quarantine transition must stop queued spans before transport export."""
    from jarvis_common import langfuse_v2_exporter as exporter_module  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

    class Response:
        status_code = 207

        @staticmethod
        def json() -> dict[str, list[object]]:
            return {"errors": []}

    class RecordingClient:
        def __init__(self, **_kwargs) -> None:
            self.batches: list[object] = []

        def post(self, _endpoint: str, *, json: object) -> Response:
            self.batches.append(json)
            return Response()

        def close(self) -> None:
            return None

    client = RecordingClient()
    monkeypatch.setattr(exporter_module.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(
        exporter_module,
        "_span_payload",
        lambda _span: (
            {"body": {"id": "trace"}},
            {"body": {"traceId": "trace"}},
        ),
    )
    exporter = exporter_module.LangfuseV2SpanExporter(
        base_url="https://langfuse.test",
        public_key="public-key",
        secret_key="secret-key",
    )

    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))

    assert exporter.export(["before"]) is SpanExportResult.SUCCESS
    assert len(client.batches) == 1

    quarantine.touch()
    assert exporter.export(["after"]) is SpanExportResult.SUCCESS
    assert len(client.batches) == 1


def test_legacy_langfuse_export_translates_only_decorated_spans():
    """The compatibility exporter must exclude generic request telemetry."""
    from types import SimpleNamespace
    from typing import cast

    from jarvis_common.langfuse_v2_exporter import _span_payload
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, TraceFlags, TraceState

    context = SpanContext(
        trace_id=1,
        span_id=2,
        is_remote=False,
        trace_flags=TraceFlags(1),
        trace_state=TraceState(),
    )
    generic = SimpleNamespace(
        attributes={"service": "research"},
        context=context,
        parent=None,
        start_time=1_000_000_000,
        end_time=2_000_000_000,
        name="http.request",
    )
    generation = SimpleNamespace(
        attributes={
            "langfuse.observation.type": "generation",
            "langfuse.observation.input": '"bounded prompt"',
            "langfuse.observation.output": '"bounded response"',
            "langfuse.observation.model.name": "smart",
        },
        context=context,
        parent=None,
        start_time=1_000_000_000,
        end_time=2_000_000_000,
        name="call_llm_structured",
    )

    assert _span_payload(cast(ReadableSpan, generic)) is None
    translated = _span_payload(cast(ReadableSpan, generation))
    assert translated is not None
    _, event = translated
    assert event["type"] == "generation-create"
    assert event["body"] == {
        "id": "0000000000000002",
        "traceId": "00000000000000000000000000000001",
        "name": "call_llm_structured",
        "startTime": "1970-01-01T00:00:01+00:00",
        "endTime": "1970-01-01T00:00:02+00:00",
        "input": "bounded prompt",
        "output": "bounded response",
        "model": "smart",
    }


def test_legacy_langfuse_export_removes_unexported_parent(monkeypatch):
    """An exported observation must not reference a filtered request span."""
    from jarvis_common import langfuse_v2_exporter as exporter_module  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

    class Response:
        status_code = 200

    class RecordingClient:
        def __init__(self) -> None:
            self.batch: dict[str, object] | None = None

        def post(self, _endpoint: str, *, json: dict[str, object]) -> Response:
            self.batch = json
            return Response()

        def close(self) -> None:
            return None

    client = RecordingClient()
    monkeypatch.setattr(exporter_module.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(
        exporter_module,
        "_span_payload",
        lambda _span: (
            {"body": {"id": "trace"}},
            {
                "body": {
                    "id": "generation",
                    "traceId": "trace",
                    "parentObservationId": "filtered-request",
                }
            },
        ),
    )
    exporter = exporter_module.LangfuseV2SpanExporter(
        base_url="https://langfuse.test",
        public_key="public-key",
        secret_key="secret-key",
    )

    assert exporter.export([object()]) is SpanExportResult.SUCCESS
    assert client.batch == {
        "batch": [
            {"body": {"id": "trace"}},
            {"body": {"id": "generation", "traceId": "trace"}},
        ]
    }


def test_legacy_langfuse_export_rejects_partial_ingestion_errors(monkeypatch):
    """A 207 response containing an event error must fail the export batch."""
    from jarvis_common import langfuse_v2_exporter as exporter_module  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

    class Response:
        status_code = 207

        @staticmethod
        def json() -> dict[str, list[dict[str, str]]]:
            return {"errors": [{"message": "rejected"}]}

    class RejectingClient:
        def post(self, _endpoint: str, *, json: object) -> Response:
            del json
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(exporter_module.httpx, "Client", lambda **_kwargs: RejectingClient())
    monkeypatch.setattr(
        exporter_module,
        "_span_payload",
        lambda _span: ({"body": {"id": "trace"}}, {"body": {"id": "observation"}}),
    )
    exporter = exporter_module.LangfuseV2SpanExporter(
        base_url="https://langfuse.test",
        public_key="public-key",
        secret_key="secret-key",
    )

    assert exporter.export([object()]) is SpanExportResult.FAILURE


# ---------------------------------------------------------------------------
# Structured-decoding mode — the instructor client must request grammar-
# constrained json_schema decoding, not prompt-only json_object.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_langfuse_hook_builds_json_schema_instructor_client(monkeypatch):
    """The shared instructor client must use Mode.JSON_SCHEMA (grammar-constrained).

    init_langfuse_hook (app_factory.py:467-473) builds the Instructor-patched
    AsyncOpenAI stored on app.state.openai_client; call_llm_structured routes
    every structured call through it. Mode.JSON_SCHEMA makes instructor emit a
    native response_format of type "json_schema", which arms the backend grammar
    engine; the old Mode.JSON emits "json_object" and prompt-injects the schema
    (the v0.9.1 schema-echo cause). instructor exposes the configured mode on the
    patched client as ``.mode``, so we assert against the real construction with
    no network call. Reverting :472 back to Mode.JSON fails this.
    """
    import types  # noqa: PLC0415

    import instructor  # noqa: PLC0415
    from jarvis_common import app_factory  # noqa: PLC0415

    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")

    app = types.SimpleNamespace(state=types.SimpleNamespace())

    await app_factory.init_langfuse_hook(app)  # type: ignore[arg-type]

    client = app.state.openai_client
    assert client.mode is instructor.Mode.JSON_SCHEMA
    assert client.mode is not instructor.Mode.JSON


# ---------------------------------------------------------------------------
# strip_think_streaming — streaming CoT filter
# ---------------------------------------------------------------------------


def test_strip_think_streaming_simple():
    """Single chunk containing a full <think>...</think> tag.

    The function may hold the trailing visible text in carry to guard against
    partial open-tags at the chunk boundary.  Callers must flush carry after
    the stream ends.  We validate out+carry equals the full visible text.
    """
    out, st, carry = strip_think_streaming("Hello <think>noise</think>World", False)
    # The filter drops the think block; visible fragments land in out or carry.
    assert out + carry == "Hello World"
    assert st is False
    assert "<think>" not in out + carry
    assert "noise" not in out + carry


def test_strip_think_streaming_split_open_tag():
    """Open tag split across two chunks: <th | ink>noise</think>."""
    out1, st1, carry1 = strip_think_streaming("Hello <th", False)
    out2, st2, carry2 = strip_think_streaming("ink>noise</think>World", st1, carry1)
    # Flush carry from final chunk to get full visible text.
    assert out1 + out2 + carry2 == "Hello World"
    assert st2 is False
    assert "noise" not in out1 + out2 + carry2


def test_strip_think_streaming_split_close_tag():
    """Close tag split across two chunks: noise</th | ink>visible."""
    out1, st1, carry1 = strip_think_streaming("Hello <think>noise</th", False)
    out2, st2, carry2 = strip_think_streaming("ink>World", st1, carry1)
    # Flush carry from final chunk to get full visible text.
    assert out1 + out2 + carry2 == "Hello World"
    assert st2 is False
    assert "noise" not in out1 + out2 + carry2


def test_strip_think_streaming_no_think():
    """No tags at all — all content must pass through (possibly via carry)."""
    out, st, carry = strip_think_streaming("Just regular text.", False)
    # All visible content must be emitted across out + carry.
    assert out + carry == "Just regular text."
    assert st is False


def test_strip_think_streaming_token_by_token():
    """Feed source string one character at a time; full output must equal filtered text."""
    src = "Hello <think>noise</think>World"
    out_buf: list[str] = []
    in_think = False
    carry = ""
    for ch in src:
        v, in_think, carry = strip_think_streaming(ch, in_think, carry)
        out_buf.append(v)
    # Flush trailing carry (only if not still inside a think block).
    if not in_think:
        out_buf.append(carry)
    result = "".join(out_buf)
    assert "noise" not in result
    assert "<think>" not in result
    # Visible segments must be present.
    assert "Hello" in result
    assert "World" in result


@pytest.mark.parametrize(
    "src",
    [
        "<think>at-start</think>visible",
        "visible<think>middle</think>",
        "<think>only-think-block</think>",
        "<think>a</think>mid<think>b</think>end",
        "no tag at all",
        "",
        "<think>multi\nline\nthought</think>after",
        "before <think>x</think> between <think>y</think> after",
    ],
)
def test_strip_think_streaming_every_byte_boundary(src):
    """Regression guard: streaming filter handles every chunk-boundary split correctly.

    For each input, feed it as two chunks split at every possible byte offset; the
    accumulated visible text (out1 + out2 + non-think carry) must equal the canonical
    regex-stripped result. Streaming was confirmed sound at the time this test
    locks that in against future refactors.
    """
    expected = re.sub(r"<think>.*?</think>", "", src, flags=re.DOTALL)
    for split_at in range(len(src) + 1):
        a, b = src[:split_at], src[split_at:]
        out1, st1, carry1 = strip_think_streaming(a, False, "")
        out2, st2, carry2 = strip_think_streaming(b, st1, carry1)
        tail = carry2 if not st2 else ""
        result = out1 + out2 + tail
        assert result == expected, (
            f"split_at={split_at}: src={src!r} got={result!r} expected={expected!r}"
        )
