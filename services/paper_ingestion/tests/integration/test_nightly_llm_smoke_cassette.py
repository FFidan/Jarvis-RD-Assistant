"""Offline cassette for the nightly real-model structured-pipeline smoke.

The nightly job (``.github/workflows/nightly-llm-smoke.yml`` →
``scripts/nightly-llm-smoke.sh``) drives every structured pipeline against the
real deployed model and fails loudly if grammar-constrained decoding silently
regresses (``llm_calls == 0`` / no parsed result). This module is its offline
twin: it runs the *same* assertion — each pipeline's response model parses out
of ``call_llm_structured`` — but with the LiteLLM ``/v1/chat/completions``
endpoint mocked via respx, so the smoke is runnable in CI with no live model.

Coverage note: the 9th structured pipeline (``card_generator`` in the
``learning_engine`` service) is NOT importable from the ``paper_ingestion``
test environment (separate service package, not on this pythonpath). It is
exercised by the live leg of ``scripts/nightly-llm-smoke.sh`` against the
deployed ``learning_engine`` service instead.
"""

from __future__ import annotations

import json

import httpx
import instructor
import openai
import pytest
import respx
from jarvis_common.llm_client import ChatCompletionOptions, call_llm_structured
from paper_ingestion.extraction.dynamic_models import _build_extraction_response_model
from paper_ingestion.extraction.kg_models import KGExtractionOutput
from paper_ingestion.models.rag import AskResponse
from paper_ingestion.pulse.models import PulseScoringOutput
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.summarization_models import SummarizationOutput
from paper_ingestion.weekly_summary_models import WeeklyDigestOutput
from pydantic import BaseModel, RootModel

pytestmark = pytest.mark.nightly_smoke

_FAKE_BASE_URL = "http://nightly-smoke-cassette:4000"
_CHAT_COMPLETIONS_URL = f"{_FAKE_BASE_URL}/v1/chat/completions"

# The dynamic extraction model is built from a fixed 2-field template so the
# cassette payload is deterministic (mirrors extraction/core.py:198 wiring).
_DynamicExtractionOutput = _build_extraction_response_model(("method", "metric"))


def _chat_completion(content: object) -> httpx.Response:
    """A valid OpenAI chat-completion whose message content is the model JSON.

    Instructor ``Mode.JSON_SCHEMA`` parses ``choices[0].message.content`` into
    the requested response model, so a structured payload here exercises the
    real parse path without a live model.
    """
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-nightly-cassette",
            "object": "chat.completion",
            "created": 0,
            "model": "smart",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(content)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


# (response_model, valid structured payload) for each paper_ingestion pipeline.
# Verified against each model definition: payloads satisfy every field
# constraint and model validator so parsing succeeds offline.
_PIPELINE_CASES: list[tuple[str, type[BaseModel] | type[RootModel], object]] = [
    (
        "pulse/scoring",
        PulseScoringOutput,
        {"relevance": 8, "novelty": 7, "reasoning": "Relevant to the topic."},
    ),
    (
        "extraction/entities",
        KGExtractionOutput,
        {
            "entities": [{"name": "BERT", "type": "method", "description": "encoder"}],
            "relationships": [
                {
                    "source": "BERT",
                    "target": "GLUE",
                    "type": "evaluates",
                    "evidence": "evaluated on the GLUE benchmark",
                    "confidence": 0.9,
                }
            ],
        },
    ),
    (
        "extraction/core",
        _DynamicExtractionOutput,
        {
            "method": {"value": "transformer", "quote": "we use a transformer"},
            "metric": {"value": "0.91", "quote": "accuracy of 0.91"},
        },
    ),
    (
        "rag/decomposition",
        RootModel[list[str]],
        ["first sub-query", "second sub-query"],
    ),
    (
        "services/summarization",
        SummarizationOutput,
        {
            "tldr": "Short.",
            "summary_brief": "Brief.",
            "summary_detailed": "Detailed.",
            "key_findings": [{"finding": "f", "quote": "q", "page_number": 1}],
            "methodology": "method",
            "limitations": None,
            "relevance_notes": None,
        },
    ),
    (
        "services/contradictions",
        ContradictionClassification,
        {
            "is_contradiction": True,
            "contradiction_type": "direct",
            "explanation": "The two findings disagree.",
            "quote_a": "Paper A reports X.",
            "quote_b": "Paper B reports not X.",
            "confidence": 0.8,
        },
    ),
    (
        "routers/rag",
        AskResponse,
        {
            "answer": "The answer.",
            "sources": [],
            "confidence": "high",
            "verified_fraction": 1.0,
            "per_sentence": [],
        },
    ),
    (
        "weekly_summary",
        WeeklyDigestOutput,
        {
            "themes": [
                {
                    "theme": "A cross-paper theme description.",
                    "supporting_papers": [1, 2],
                    "notes": None,
                }
            ],
            "summary": "A sufficiently long executive summary of the week.",
        },
    ),
]


def _patched_client() -> openai.AsyncOpenAI:
    return instructor.from_openai(
        openai.AsyncOpenAI(base_url=f"{_FAKE_BASE_URL}/v1", api_key="dummy"),
        mode=instructor.Mode.JSON_SCHEMA,
    )


@respx.mock
@pytest.mark.parametrize("pipeline,response_model,payload", _PIPELINE_CASES)
async def test_structured_pipeline_parses_offline(pipeline, response_model, payload):
    """Each structured pipeline parses a valid cassette response to its model."""
    route = respx.post(_CHAT_COMPLETIONS_URL).mock(return_value=_chat_completion(payload))

    result = await call_llm_structured(
        _patched_client(),
        response_model=response_model,
        prompt="structured smoke probe",
        options=ChatCompletionOptions(model="fast", system="Return JSON."),
    )

    assert route.called, f"{pipeline}: structured call never reached /v1/chat/completions"
    assert isinstance(result, response_model), f"{pipeline}: parsed to {type(result).__name__}"
