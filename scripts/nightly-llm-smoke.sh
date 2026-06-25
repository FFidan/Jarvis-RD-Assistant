#!/usr/bin/env bash
# Nightly real-model structured-pipeline smoke.
#
# The "live-then-gate" control for grammar-constrained decoding: drive every
# structured pipeline once and FAIL (non-zero exit = a red nightly run = the
# visible signal) if any structured call yields no parsed result — i.e. the
# CI-layer analogue of Pulse's in-app `llm_calls == 0` degraded warning.
#
# Two legs:
#   1. Offline cassette (ALWAYS) — respx-mocked /v1/chat/completions; proves the
#      parse path works with no live model, so the smoke is runnable in CI.
#   2. Live (only when LITELLM_BASE_URL is set) — drives each of the 9 structured
#      pipelines against the real deployed model and asserts a parsed result.
#
# Assumes the Docker stack (ollama + litellm) is already running on the
# self-hosted runner (it is on REDACTED-HOST).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_DIR="$ROOT_DIR/services/paper_ingestion"
LE_DIR="$ROOT_DIR/services/learning_engine"

run_cassette_leg() {
  echo "=== Nightly LLM smoke: offline cassette leg ==="
  (
    cd "$PI_DIR"
    uv run pytest -m nightly_smoke \
      tests/integration/test_nightly_llm_smoke_cassette.py -q
  )
}

# Three-state gate for the live leg:
#   (a) LITELLM_BASE_URL empty/unset -> live leg NOT configured: run cassette
#       only and exit with the cassette's status. Honest cassette-only, never a
#       false live pass.
#   (b) LITELLM_BASE_URL set but the endpoint is unreachable -> LOUD FAILURE:
#       exit non-zero. We never silently downgrade a configured live leg to a
#       cassette-only pass (the original "safety net that validated nothing").
#   (c) LITELLM_BASE_URL set and reachable -> run cassette then the live leg.
if [[ -z "${LITELLM_BASE_URL:-}" ]]; then
  echo "=== Nightly LLM smoke: LITELLM_BASE_URL unset — live leg NOT configured ==="
  echo "    To enable the live leg, set the LITELLM_SMOKE_BASE_URL repo variable"
  echo "    (e.g. http://localhost:4000) on the self-hosted runner where the stack runs."
  run_cassette_leg
  echo "=== Nightly LLM smoke: PASS (cassette only — live leg not configured) ==="
  exit 0
fi

# Reachability probe: LiteLLM exposes an unauthenticated liveness endpoint.
# If the configured endpoint cannot be reached, fail loudly — a configured-but-
# unreachable live leg MUST surface as a red run, not a silent cassette pass.
echo "=== Nightly LLM smoke: probing LiteLLM at $LITELLM_BASE_URL ==="
if ! curl --fail --silent --show-error --max-time 10 \
    "${LITELLM_BASE_URL%/}/health/liveliness" >/dev/null 2>&1; then
  echo "=== Nightly LLM smoke: FAIL — LITELLM_BASE_URL=$LITELLM_BASE_URL is set but unreachable ===" >&2
  echo "    The live leg is configured but the LiteLLM endpoint did not respond at" >&2
  echo "    ${LITELLM_BASE_URL%/}/health/liveliness. Refusing to silently pass." >&2
  echo "    On the self-hosted runner, verify the Docker stack (ollama + litellm) is up" >&2
  echo "    and that LITELLM_SMOKE_BASE_URL points at the host-published port" >&2
  echo "    (docker-compose.yml publishes litellm at 127.0.0.1:4000)." >&2
  exit 1
fi
echo "=== Nightly LLM smoke: LiteLLM reachable ==="

run_cassette_leg

echo "=== Nightly LLM smoke: live leg (LITELLM_BASE_URL=$LITELLM_BASE_URL) ==="

# 8 paper_ingestion structured pipelines, driven once each against the real
# model. A non-parse (RuntimeError / validation failure) propagates and exits
# non-zero — the visible regression signal.
(
  cd "$PI_DIR"
  PYTHONPATH=".:../../libs/jarvis_common" uv run python - <<'PY'
import asyncio

import openai
import instructor
from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from jarvis_common.llm_client import (
    ChatCompletionOptions,
    call_llm_structured,
    get_litellm_config,
)
from pydantic import RootModel

from paper_ingestion.extraction.dynamic_models import _build_extraction_response_model
from paper_ingestion.extraction.kg_models import KGExtractionOutput
from paper_ingestion.models.rag import AskResponse
from paper_ingestion.pulse.models import PulseScoringOutput
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.summarization_models import SummarizationOutput
from paper_ingestion.weekly_summary_models import WeeklyDigestOutput

_DYNAMIC = _build_extraction_response_model(("method", "metric"))

PIPELINES = [
    ("pulse/scoring", PulseScoringOutput, "smart"),
    ("extraction/entities", KGExtractionOutput, "fast"),
    ("extraction/core", _DYNAMIC, "fast"),
    ("rag/decomposition", RootModel[list[str]], "fast"),
    ("services/summarization", SummarizationOutput, "fast"),
    ("services/contradictions", ContradictionClassification, "fast"),
    ("routers/rag", AskResponse, "fast"),
    ("weekly_summary", WeeklyDigestOutput, "fast"),
]

PROMPT = (
    "Reply with a minimal JSON object that satisfies the requested schema, "
    "using short placeholder values for every required field."
)


async def main() -> None:
    config = get_litellm_config()
    client = instructor.from_openai(
        openai.AsyncOpenAI(base_url=f"{config.base_url}/v1", api_key="dummy"),
        mode=instructor.Mode[STRUCTURED_DECODING_MODE],
    )
    for name, model, alias in PIPELINES:
        result = await call_llm_structured(
            client,
            response_model=model,
            prompt=PROMPT,
            options=ChatCompletionOptions(model=alias, system="Return JSON."),
        )
        if not isinstance(result, model):
            raise SystemExit(f"FAIL {name}: structured call returned no parsed result")
        print(f"OK {name}: parsed {type(result).__name__}")


asyncio.run(main())
PY
)

# 9th structured pipeline (card_generator) lives in the learning_engine service.
(
  cd "$LE_DIR"
  PYTHONPATH=".:../../libs/jarvis_common" uv run python - <<'PY'
import asyncio

import openai
import instructor
from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from jarvis_common.llm_client import (
    ChatCompletionOptions,
    call_llm_structured,
    get_litellm_config,
)

from learning_engine.card_models import CardGenerationOutput

PROMPT = (
    "Reply with a minimal JSON object containing one flashcard that satisfies "
    "the requested schema, using short placeholder values for every field."
)


async def main() -> None:
    config = get_litellm_config()
    client = instructor.from_openai(
        openai.AsyncOpenAI(base_url=f"{config.base_url}/v1", api_key="dummy"),
        mode=instructor.Mode[STRUCTURED_DECODING_MODE],
    )
    result = await call_llm_structured(
        client,
        response_model=CardGenerationOutput,
        prompt=PROMPT,
        options=ChatCompletionOptions(model="fast", system="Return JSON."),
    )
    if not isinstance(result, CardGenerationOutput):
        raise SystemExit("FAIL card_generator: structured call returned no parsed result")
    print(f"OK card_generator: parsed {type(result).__name__}")


asyncio.run(main())
PY
)

echo "=== Nightly LLM smoke: PASS (cassette + live) ==="
