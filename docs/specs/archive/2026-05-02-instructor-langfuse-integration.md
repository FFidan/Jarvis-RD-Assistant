# Instructor + Langfuse Integration — Design Spec
**Status:** RATIFIED 2026-05-02
**Date:** 2026-05-02
**Scope:** Marathon Phase B.1 (Instructor) + B.2 (Langfuse) combined
**Supersedes (in scope only):** B.1 and B.2 sub-specs in [docs/plans/2026-04-30-marathon-meta.md](../plans/2026-04-30-marathon-meta.md) §"Phase B — Library Modernization (Detailed)"
**Author:** Brainstormed with Claude (Opus 4.7) against ground-truth code reads of all 6 LLM call sites + `llm_client.py` + ARCHITECTURE/ENGINEERING_STANDARDS docs.

---

## 0. Why This Spec Exists (and why B.1 + B.2 are one sprint)

The META plan listed Instructor (B.1) and Langfuse (B.2) as separate sprints because they're distinct concerns: typed structured outputs vs. observability. In practice, both wrap **the same six functions** at **the same choke point** (`libs/jarvis_common/jarvis_common/llm_client.py`) with **the same blast radius** (every existing `call_llm` / `call_llm_json_value` site). Doing them sequentially means touching each call site twice — first to add a Pydantic `response_model=`, then a week later to add `@observe()`. That's wasted churn for zero independent value: a Langfuse trace that doesn't capture the structured output is incomplete, and an Instructor refactor without traces is undebuggable.

Combining them also collapses the highest-risk step — the Instructor + LiteLLM + Ollama compatibility unknown — into a single integration test that simultaneously clears the Langfuse trace path. One pre-flight test, one risk gate, one cutover.

The product is **pre-launch** → atomic cutover, no shims, no parallel APIs lingering past the sprint.

---

## 1. Goals and Non-Goals

### 1.1 Goals
1. Replace all manual `json.loads` / `dict.get` access on LLM responses with strict Pydantic models, validated by Instructor, with auto-retry on validation failure (max 2 retries).
2. Wrap every LLM call site (structured + streaming) with Langfuse `@observe()` so a Pulse run, a RAG question, an extraction batch, and a card-generation job each produce a complete trace tree.
3. Self-host Langfuse via `docker-compose --profile observability` (off by default; opt-in only).
4. Delete `call_llm` and `call_llm_json_value` from `jarvis_common/llm_client.py` in the cutover commit. No deprecation period.

### 1.2 Explicit non-goals
- DSPy programmatic prompt optimization. (No benchmark dataset; deferred per META Phase D.)
- Pydantic AI agentic features. (No agent flows yet; deferred per META Phase D.)
- Migrating embedding paths. (`embed_texts()` returns vectors, not LLM completions — out of scope.)
- Restructuring prompt builders. (`pulse/prompts.py`, `weekly_summary.DIGEST_PROMPT`, `card_generator.CARD_GENERATION_PROMPT`, etc. keep their current shape; only the response side changes.)

---

## 2. Architectural Choke Point

All structured LLM use today flows through three functions in [llm_client.py](../../libs/jarvis_common/jarvis_common/llm_client.py):

| Function | Role | After this sprint |
|---|---|---|
| `request_chat_completion_content` | Raw chat completion → str | **Kept** (used by streaming RAG, decomposition, etc.) |
| `call_llm` | Strict JSON object → `dict[str, Any]` | **Deleted** |
| `call_llm_json_value` | JSON scalar/array/object → `Any` | **Deleted** |

Replaced by:

```python
async def call_llm_structured(
    http_client: httpx.AsyncClient,
    *,
    response_model: type[T],          # T is a Pydantic BaseModel subclass
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions | None = None,
    config: LiteLLMConfig | None = None,
    max_retries: int = 2,
) -> T: ...
```

`T` is a typed Pydantic model. The function patches an `openai.AsyncOpenAI(base_url=litellm.base_url)` client with `instructor.from_openai(client, mode=Mode.JSON)` and calls `chat.completions.create(response_model=T, max_retries=max_retries)`. Because LiteLLM exposes an OpenAI-compatible `/v1/chat/completions` endpoint (already used today at [llm_client.py:107](../../libs/jarvis_common/jarvis_common/llm_client.py#L107)), the OpenAI client speaks to it natively — we do not import `litellm` as a Python dep.

**Why `Mode.JSON` and not `Mode.TOOLS`:** Ollama's tool-calling support is uneven across models; JSON mode just requires the model to honor `response_format={"type": "json_object"}`, which mistral-nemo (the current "smart" alias) and qwen3.5 (the "fast" alias) both do today. The compatibility test (§3) verifies this end-to-end before a single production call site is touched.

**Langfuse wrapping:** `call_llm_structured` is decorated with `@observe(as_type="generation")`, capturing model, input messages, output (the validated Pydantic instance, serialized via `model_dump_json`), and latency. Outer call sites get `@observe()` (default span type) to define **trace boundaries** (one Pulse run = one trace; one RAG question = one trace; one extraction batch = one trace). Langfuse SDK is initialized once in each FastAPI service's lifespan via `configure_lifespan`.

---

## 3. Compatibility Integration Test (Wave 1 critical path)

**This is the single most important step in the sprint.** Per META B.1: "Instructor + LiteLLM + Ollama. The Instructor docs say it works with Ollama, but the LiteLLM gateway path needs a one-time integration test." We add the Langfuse trace check to the same test so both libraries are validated together.

**Path:** `services/paper_ingestion/tests/integration/test_instructor_langfuse_litellm.py`
**Marker:** `@pytest.mark.integration` (excluded from default repo-root pytest; Docker-backed lane runs it)
**Length target:** ≤70 lines including imports.

**Coverage:**

```python
# Pseudocode shape (real test goes in the file above)
import instructor
from openai import AsyncOpenAI
from langfuse.decorators import langfuse_context, observe
from pydantic import BaseModel, Field, ValidationError

class _ScoringProbe(BaseModel):
    relevance: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    reasoning: str = Field(min_length=1, max_length=200)

@pytest.mark.integration
async def test_instructor_litellm_ollama_happy_path():
    client = instructor.from_openai(
        AsyncOpenAI(base_url="http://litellm:4000", api_key="dummy"),
        mode=instructor.Mode.JSON,
    )
    result = await client.chat.completions.create(
        model="smart",
        response_model=_ScoringProbe,
        max_retries=2,
        messages=[{"role": "user", "content":
            "Score this fake paper. Return JSON with relevance=7, novelty=5, "
            "reasoning='ok'."}],
    )
    assert isinstance(result, _ScoringProbe)
    assert 1 <= result.relevance <= 10

@pytest.mark.integration
async def test_instructor_validation_retry_on_bad_output():
    # Force a tight constraint the model is likely to fail once
    class _Strict(BaseModel):
        n: int = Field(ge=1, le=3)
    # Expect either success-after-retry OR ValidationError after retries exhausted
    ...

@pytest.mark.integration
@observe()
async def test_langfuse_trace_emission():
    # Run a structured call inside an @observe() boundary, then poll the
    # Langfuse public-key API for the trace ID. Skip if LANGFUSE_HOST unset.
    ...
```

**Acceptance:** all three tests green against the project's Docker Compose stack (`litellm` + `ollama` + `langfuse`). The third may be skipped in CI if Langfuse isn't running, but must be runnable locally.

**Hard rule:** no production call site is migrated until these three tests pass green at least once on the developer's machine. The Wave-1 canary (§5.1 below) is the second gate.

---

## 4. Per-Site Refactor Plans

Six call sites total. Pydantic models below define the **wire-shape contract** between the LLM and our code; existing storage models (`ExtractedField`, `EntityExtractionResponse`, `PaperContradictionResponse`, etc.) are unchanged — the LLM-output models translate into them at the call site as today.

### 4.1 Site 1: `pulse/scoring.py` — stage-2 reranker (CANARY)

**Current** ([scoring.py:282](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L282)): `call_llm` → `dict[str, Any]` → `int(parsed["relevance"])`, `int(parsed["novelty"])`, `str(parsed.get("reasoning", ""))`. Three KeyError/ValueError catches today.

**New model** (in `paper_ingestion/pulse/models.py` — new file):

```python
class PulseScoringOutput(BaseModel):
    """LLM output shape for stage-2 candidate reranking."""
    relevance: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    reasoning: str = Field(min_length=1, max_length=400)
```

**Migration delta:** the `parsed = await call_llm(...)` block becomes `parsed = await call_llm_structured(..., response_model=PulseScoringOutput, messages=scoring_messages, ...)`. The post-LLM clamp logic (`max(1, min(10, relevance))`) is dropped — Field validators enforce it. The existing `try/except (ValueError, RuntimeError, httpx.HTTPError, KeyError, TypeError)` block widens to also catch `pydantic.ValidationError` (the failure mode after retries exhausted) and continues to return the same graceful-degradation `ScoredCandidate(... reasoning="LLM scoring failed", ...)` shape.

**Why this is the canary:** smallest model (3 fields, no nesting), tightest field constraints (so retry behavior is exercised), most-run path (every Pulse candidate hits it). If Instructor + Ollama works for this, the rest is mechanical.

### 4.2 Site 2: `extraction/core.py` — template-driven field extraction (DYNAMIC SCHEMA)

**Current** ([core.py:157](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L157)): user-defined templates → response shape is `{<field_name>: {"value": Any, "quote": str|None}}` where keys are dynamic per template.

**Approach:** dynamic Pydantic model construction via `pydantic.create_model`. This is the trickiest of the six sites because Instructor needs a concrete class per call.

**New shape:**

```python
class ExtractedFieldOutput(BaseModel):
    value: str | int | float | None = Field(
        description="Extracted value or null if not found in source"
    )
    quote: str | None = Field(
        default=None,
        description="Verbatim quote — must be exact substring of source text"
    )

def _build_extraction_response_model(template_fields: list[dict]) -> type[BaseModel]:
    """Build a per-template Pydantic model from the template's field list."""
    fields_kwargs = {
        f["name"]: (ExtractedFieldOutput | None, Field(default=None))
        for f in template_fields
    }
    return create_model(
        f"PaperExtractionOutput_{hash(tuple(f['name'] for f in template_fields)) & 0xFFFF}",
        **fields_kwargs,
    )
```

**Migration delta:** before `call_llm_structured`, build the per-template model once and cache it on the `extraction_templates` row (or by `template_id` in a process-local LRU). After the call, iterate `model_dump()` keys exactly as today, feed each `ExtractedFieldOutput` into the existing `ExtractedField` storage model with the verifier flow unchanged. Dropped values stay dropped (`value=None`) — validation cannot enforce a constraint that requires source-text inspection, so the QuoteVerifier path stays.

**Risk note:** field names that aren't valid Python identifiers must be sanitized before `create_model`. Templates today don't validate field names against this constraint — add a Pydantic validator on `ExtractionField.name` (in `models/extractions.py`) requiring `re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name)` so existing templates that violate it surface at template-create time, not at extraction time.

### 4.3 Site 3: `extraction/entities.py` — KG entity + relationship extraction

**Current** ([entities.py:288](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L288)): `call_llm` → `dict` with `"entities"` (list of dicts) and `"relationships"` (list of dicts). Per-entity / per-relationship validation today is inline isinstance + .get-with-default checks.

**New model:**

```python
class KGEntityCandidate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["method", "dataset", "metric", "concept", "institution", "author"]
    description: str | None = Field(default=None, max_length=500)

class KGRelationshipCandidate(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: Literal["used_on", "outperforms", "extends", "evaluates", "proposes", "affiliated_with"]
    evidence: str = Field(min_length=10, description="Verbatim evidence quote")

class KGExtractionOutput(BaseModel):
    entities: list[KGEntityCandidate] = Field(default_factory=list, max_length=15)
    relationships: list[KGRelationshipCandidate] = Field(default_factory=list, max_length=10)
```

**Migration delta:** the `valid_types` set check at [entities.py:300](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L300) is replaced by the `Literal[...]` constraint — Instructor's retry will pressure the model to use the canonical type names. Relationship-type validation (currently absent — the code accepts any string at line 384) becomes enforced. The downstream `_find_or_create_entity` flow, embedding precomputation, and `QuoteVerifier` path are unchanged.

**Tightening note:** today the code allows arbitrary `rel.get("type")` strings to flow into the `entity_relationships.relationship_type` column. The Literal constraint is a tightening, not a no-op. If any downstream code reads relationship types that aren't in the six values above, it must be inventoried before the cutover. Quick grep for `relationship_type` in the codebase to verify before merging.

### 4.4 Site 4: `learning_engine/card_generator.py` — flashcard generation

**Current** ([card_generator.py:119](../../services/learning_engine/learning_engine/card_generator.py#L119)): `call_llm` → `dict` with `"cards"` (list of dicts). Existing `VALID_CARD_TYPES = frozenset({"concept", "quote", "method", "comparison"})` set membership replaced by `Literal`.

**New model:**

```python
class CardOutput(BaseModel):
    card_type: Literal["concept", "quote", "method", "comparison"]
    front: str = Field(min_length=10, max_length=500, description="Question text")
    back: str = Field(min_length=5, max_length=2000, description="Answer text")
    evidence_quote: str = Field(min_length=20, description="Verbatim quote")
    page_number: int | None = Field(default=None, ge=1)

class CardGenerationOutput(BaseModel):
    cards: list[CardOutput] = Field(min_length=1, max_length=20)
```

**Migration delta:** `_call_llm_for_cards` returns `CardGenerationOutput | None` instead of `list[dict] | None`. The `_verify_raw_cards` method takes `list[CardOutput]` — its body changes from `card.get("evidence_quote", "")` to `card.evidence_quote`. The 100%-fail abstract-fallback (rule 6) and rule-5 confidence computation are unchanged. `VALID_CARD_TYPES` and the defensive `if card_type not in VALID_CARD_TYPES: card_type = "concept"` clamp are deleted — Pydantic enforces the type at parse time.

### 4.5 Site 5: `services/contradictions.py` — contradiction classifier

**Current** ([contradictions.py:516](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L516)): `call_llm` → `dict` with mixed-shape output (when `is_contradiction=False`, quotes can be empty; when `is_contradiction=True`, quotes must be non-empty). Currently enforced by `if not parsed.get("is_contradiction"): return None` then string-emptiness checks.

**New model:**

```python
class ContradictionClassification(BaseModel):
    is_contradiction: bool
    contradiction_type: Literal["direct", "methodological", "result", "interpretation"] = "direct"
    explanation: str = Field(min_length=10, max_length=400)
    quote_a: str = Field(default="")
    quote_b: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _quotes_required_if_contradiction(self) -> "ContradictionClassification":
        if self.is_contradiction and (not self.quote_a.strip() or not self.quote_b.strip()):
            raise ValueError(
                "is_contradiction=True requires non-empty quote_a and quote_b"
            )
        return self
```

**Migration delta:** the `_classify_candidate` return becomes `ContradictionClassification | None` (None when `is_contradiction=False`, instance otherwise). The validator forces Instructor to retry when the model returns `is_contradiction=True` with empty quotes — that's a stricter contract than today's silent-skip. Caller `scan_contradictions` reads `.quote_a`, `.quote_b`, `.contradiction_type`, `.confidence`, `.explanation` directly.

### 4.6 Site 6: `weekly_summary.py` — per-topic digest (UNTOUCHED IN META TOP-5)

**Current** ([weekly_summary.py:178](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L178)): `call_llm` → `dict` with `"themes"` (list of dicts) and `"summary"` (str). Single per-topic call inside a loop.

**New model:**

```python
class ThemeOutput(BaseModel):
    theme: str = Field(min_length=10, max_length=300)
    supporting_papers: list[int] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)

class WeeklyDigestOutput(BaseModel):
    themes: list[ThemeOutput] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=20, max_length=600)
```

**Migration delta:** the `try/except Exception: logger.exception(...)` block at [weekly_summary.py:195](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L195) widens to also catch `pydantic.ValidationError`; the graceful-degradation branch (default `summary` text, empty `themes`) is unchanged.

### 4.7 Streaming and scalar paths (Langfuse-only, no Instructor)

These do **not** get Pydantic models — they keep raw string/scalar responses. Instructor is structured-output-only; streaming and scalar JSON aren't its problem domain.

| Site | File:line | Change |
|---|---|---|
| RAG streaming | [rag/streaming.py](../../services/paper_ingestion/paper_ingestion/rag/streaming.py) (uses `request_chat_completion_content` streaming variant) | Add `@observe(as_type="generation")` only |
| Query decomposition | [rag/decomposition.py:61](../../services/paper_ingestion/paper_ingestion/rag/decomposition.py#L61) (`call_llm_json_value`, scalar list[str]) | Add `@observe()`; keep `call_llm_json_value` until site is rewritten using `RootModel[list[str]]` (small follow-up — out of scope this sprint OR included in Wave 3 cleanup; see §5.3) |
| Embedding requests | [llm_client.py:201](../../libs/jarvis_common/jarvis_common/llm_client.py#L201) (`embed_texts`) | Add `@observe(as_type="generation")` with model="embed", capture token count if available |

**Decomposition note:** because the sprint goal is "delete `call_llm_json_value`", the cleanest path is to migrate `decomposition.py` too in Wave 3 with a `RootModel[list[str]]` response model. That's one extra refactor for a clean cutover. Recommended.

---

## 5. Migration Order and Waves

The user's META directive: **start with `pulse/scoring.py` (smallest blast radius, biggest win)**. Three waves over ~5 working days.

### 5.1 Wave 1 — Compatibility + canary (M–T, ~2 days)

| Step | File | Acceptance |
|---|---|---|
| Add `instructor>=1.5`, `openai>=1.50`, `langfuse>=2.50` | `libs/jarvis_common/pyproject.toml` | `uv sync` clean; `bash scripts/check-python-deps.sh` clean |
| Add Langfuse service block | `docker-compose.yml` (`profiles: [observability]`) | `docker compose --profile observability up -d langfuse` healthy |
| Add `call_llm_structured` | `libs/jarvis_common/jarvis_common/llm_client.py` | New unit test in `libs/jarvis_common/tests/test_llm_client_structured.py` covering happy path with mocked openai client |
| Initialize Langfuse SDK in lifespan | `libs/jarvis_common/jarvis_common/app_factory.py` (or per-service `main.py`) | Trace appears in dashboard for one canary call |
| Write integration test (§3) | `services/paper_ingestion/tests/integration/test_instructor_langfuse_litellm.py` | All three integration tests pass against running Docker stack |
| **Refactor `pulse/scoring.py`** | `services/paper_ingestion/paper_ingestion/pulse/scoring.py` + new `pulse/models.py` | Existing pulse tests pass; one Pulse run end-to-end produces a Langfuse trace tree |

**Wave 1 gate:** integration test green AND scoring.py canary in production with Langfuse traces. **Do not enter Wave 2 if either fails.**

### 5.2 Wave 2 — Bulk refactor (W–Th, ~2 days)

Five remaining call sites. Each gets its own commit (atomic, reviewable) but all merge in one PR (atomic cutover):

1. `extraction/core.py` (dynamic-schema model — highest complexity in this wave)
2. `extraction/entities.py` (Literal-typed entities + relationships)
3. `learning_engine/card_generator.py` (cross-service — verify learning_engine imports `call_llm_structured` correctly)
4. `services/contradictions.py` (model_validator for cross-field invariant)
5. `weekly_summary.py` (smallest delta)

Each commit also adds the appropriate `@observe()` decorator at the call-site boundary (e.g., on `extract_fields_for_paper`, on `extract_entities_for_paper`, on `CardGenerator.generate_cards`, on `scan_contradictions`, on `generate_weekly_summary`). Trace boundaries are these top-level functions; Instructor's `@observe(as_type="generation")` on `call_llm_structured` is the leaf span inside them.

### 5.3 Wave 3 — Cleanup + cutover (F, ~1 day)

| Step | Acceptance |
|---|---|
| Migrate `rag/decomposition.py` to `call_llm_structured(response_model=RootModel[list[str]])` | `decompose_query` returns same shape; existing tests pass |
| Add `@observe()` to RAG streaming, embedding, decomposition | Traces appear for full RAG-question lifecycle |
| Settings UI: add link to Langfuse dashboard URL | Frontend test verifies link present when `VITE_LANGFUSE_PUBLIC_DASHBOARD` env var set |
| **Delete `call_llm` and `call_llm_json_value`** from `llm_client.py` | grep across `services/`, `libs/`, `scripts/` returns zero hits; pyright 0/0 |
| **Update all docs in §6** | `python3 scripts/check_agent_docs.py` clean |

**Wave 3 gate:** quality gates from §7 + atomic doc cutover in same commit.

---

## 6. Decision: Strict-JSON with Retry vs Fallback Layer

**Decision: Instructor strict mode with `max_retries=2`, plus the existing per-site `try/except` for graceful degradation. No separate manual-parse fallback layer.**

### 6.1 Options considered

| Option | Description | Verdict |
|---|---|---|
| A. Strict Instructor, no retry | `max_retries=0`; `ValidationError` bubbles out to call site | Rejected — fragile against transient malformations Ollama produces |
| B. **Strict Instructor, `max_retries=2`** (chosen) | Instructor re-feeds the validation error to the LLM and asks for a corrected response; bubbles `ValidationError` only after retries exhausted | Recommended |
| C. Hybrid: Instructor first, then manual `json.loads + dict.get` if Instructor fails | Two parsing paths; on Instructor failure, attempt the legacy permissive parse | Rejected — pre-launch project, "no shims" constraint; doubles surface area that can drift; defeats the type-safety win |
| D. Permissive Instructor (`Mode.JSON_SCHEMA` with `nullable=True` on every field) | Accept any-shape output, validate post-hoc | Rejected — same problem as today's `dict.get` access; no static-analysis benefit |

### 6.2 Why B wins

1. **Retry beats fallback for the failure mode that actually happens.** Per-site experience (read across all 6 sites) shows the dominant failure is "LLM returned almost-valid JSON" — missing one nested key, wrong type for a numeric field, off-by-one on a Literal value. Instructor's retry feeds the exact `ValidationError` message back to the LLM, which corrects it >90% of the time on local Mistral-Nemo. A manual-parse fallback can't do this — it just relaxes the contract.

2. **The graceful-degradation pattern is unchanged.** Every site today already wraps its LLM call in `try/except` returning a sentinel result (skip this candidate, drop these themes, etc.). After this sprint, that `except` clause includes `pydantic.ValidationError` — one-line change. No new error-handling layer.

3. **"No shims" is a load-bearing constraint.** A second parse path is a shim by definition. Pre-launch atomic cutover means we delete `call_llm` rather than letting it linger as a fallback.

4. **Static-analysis wins.** Pyright catches `card.front` typos at edit-time; `card.get("front", "")` lets typos slide. That's the single biggest engineering win and would be erased by a permissive fallback.

### 6.3 Cost ceiling for retries

`max_retries=2` means up to 3 LLM round-trips per call site at the worst. For Pulse (top_k=50 candidates, bounded concurrency 5), the worst-case theoretical added latency is `2 × per_call_latency × ceil(50/5) = ~20 × per_call_latency` seconds — but in practice retries fire on a small fraction of calls. We measure this against the canary before Wave 2 commits. **Retries above 2 are not added without a recorded latency budget review.**

---

## 7. Verification (quality gates)

Inherits from [docs/ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md). Specific to this sprint:

- `uv run ruff check services/ libs/ scripts/` — clean
- `uv run pytest` — full pass; no skips on previously-passing tests
- `uv run pyright services/ libs/` — 0 errors / 0 warnings
- `npm --prefix frontend run lint` + `npm --prefix frontend run test -- --run` + `npm --prefix frontend run typecheck` — clean (only Settings UI link is touched)
- `bash scripts/check-python-deps.sh` — service requirements regenerated and committed
- `python3 scripts/check_agent_docs.py` — clean after doc updates land
- Integration test `test_instructor_langfuse_litellm.py` green against running Docker stack
- Manual smoke (Wave 1 gate, Wave 3 close): one Pulse run, one RAG question, one card-gen job, one extraction batch — all four show complete trace trees in Langfuse dashboard
- grep audit (Wave 3 close): `grep -rn "call_llm\b\|call_llm_json_value\b" services/ libs/ scripts/` returns zero hits in production code (test fixtures referencing deleted symbols are also cleaned)

---

## 8. Documentation Updates (atomic with code)

Per META Phase B doc-inventory matrix, expanded for the combined scope:

| Doc | Change |
|---|---|
| [README.md](../../README.md) | Tech Stack section: add Instructor + Langfuse rows; new "Optional observability profile" subsection in setup |
| [docs/ARCHITECTURE.md](../ARCHITECTURE.md) | Replace LLM-client subsection with: `call_llm_structured` as the only LLM entry point; trace boundary policy (Pulse run = trace, RAG question = trace, extraction batch = trace, card gen = trace); Langfuse opt-in via `--profile observability` |
| [CLAUDE.md](../../CLAUDE.md) | New §"LLM call pattern": always use `call_llm_structured(response_model=...)`; never `dict.get` on LLM output; subagent prompts must include the response_model class definition or a snippet |
| [AGENTS.md](../../AGENTS.md) | One-line addition to Required Docs / patterns table referencing this spec |
| [docs/DEPLOYMENT.md](../DEPLOYMENT.md) | New §"Observability (optional)": how to start the Langfuse profile, env vars (`LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`), Settings UI link wiring |
| [docs/REQUIREMENTS.md](../REQUIREMENTS.md) | Add Instructor + Langfuse + openai client to Python deps section |
| [docs/CHANGELOG.md](../CHANGELOG.md) | New entry: "B.1+B.2 — Instructor + Langfuse integration" |
| [.env.example](../../.env.example) | Add `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (commented-out by default) |
| [PERSONAL-SETUP.md](../../PERSONAL-SETUP.md) | One-line note about optional observability profile |

The lifecycle-redesign spec ([2026-04-29-paper-lifecycle-redesign.md](2026-04-29-paper-lifecycle-redesign.md)) needs no change — it called this out in §16 ("Library appendix") and the schemas it defined are intentionally compatible with Instructor.

---

## 9. Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Instructor `Mode.JSON` fails on Ollama for some models | HIGH | Integration test §3 is the gate before any production refactor. If it fails: switch to manual `response_format={"type": "json_object"}` + Pydantic `model_validate` (keeps Pydantic typing, drops Instructor's retry magic). |
| Dynamic-schema in `extraction/core.py` produces brittle Pydantic models for templates with weird field names | MEDIUM | Add field-name regex validator to `ExtractionField.name` at template-create time (§4.2). Backfill check: scan existing templates pre-cutover. |
| Langfuse self-hosted Postgres conflicts with project Postgres on a shared port / volume | MEDIUM | Use named profile, separate volume (`langfuse-postgres-data`), separate port (default 3030 for Langfuse UI). Document in DEPLOYMENT.md. |
| Retry budget blows up Pulse latency | MEDIUM | Measure during Wave 1 canary; if p95 added latency > 30s for a Pulse run, drop `max_retries` to 1 and document. |
| Tighter Literal constraints (e.g., relationship_type in §4.3) reject existing valid free-text values from old data | LOW | Migration not affected (only new LLM outputs are validated); pre-cutover grep verifies no downstream code reads non-Literal types. |
| Cross-service import: `learning_engine` consumes `call_llm_structured` from `jarvis_common` | LOW | Existing pattern — `card_generator.py` already imports from `jarvis_common.llm_client`. No new boundary crossing. |
| Frontend Settings link to Langfuse needs an env var to render | LOW | Treat as a presentational change; gated by `VITE_LANGFUSE_PUBLIC_DASHBOARD`; absent → don't render. |

---

## 10. Out of Scope

- DSPy, Pydantic AI, BERTopic, Outlines (per META Phase D / spec §16).
- Replacing `request_chat_completion_content` for streaming RAG. Streaming is intrinsically not structured-output — it stays raw.
- Rewriting prompt builders. The system/user message text in `pulse/prompts.py`, `weekly_summary.DIGEST_PROMPT`, `card_generator.CARD_GENERATION_PROMPT`, etc. is unchanged. (Instructor injects schema instructions automatically — we may revisit prompt minimization in a follow-up.)
- Telegram bot LLM calls (none today).
- B.3 (mxbai-rerank) and B.4 (Taskiq) — separate specs.
- Cost telemetry. Langfuse can capture token counts if the underlying API returns them; LiteLLM does — but per-trace cost dashboards are a Phase-B follow-up, not this sprint.

---

## 11. Open Questions for User Review

1. **Combined-spec format** — proceed with this single doc, or split-and-share-the-test? My pick: this single doc. *No further decision needed unless you want it split.*
2. **Wave 3 decomposition migration** — include `decompose_query` in this sprint (clean cutover) or defer? My pick: include. *You may say defer.*
3. **Langfuse self-hosted vs cloud** — spec assumes self-hosted via Docker Compose. Cloud is also free for low volume. *Confirm self-hosted.*
4. **Settings-UI link** — gated on env var. *Confirm we want this in B.2 scope vs deferred.*
5. **Instructor retry budget = 2** — confirm OR raise/lower the cap.

---

## Verified Identifiers

Every cited identifier was Read in this session via the Read tool against HEAD (`master` @ `7930f0b`).

| Citation | File:line | Behavior |
|---|---|---|
| `call_llm` | [libs/jarvis_common/jarvis_common/llm_client.py:179-198](../../libs/jarvis_common/jarvis_common/llm_client.py#L179-L198) | Async wrapper that requests a chat completion with `response_format={"type":"json_object"}` and returns `dict[str,Any]`; raises `ValueError` on non-object JSON |
| `call_llm_json_value` | [libs/jarvis_common/jarvis_common/llm_client.py:130-176](../../libs/jarvis_common/jarvis_common/llm_client.py#L130-L176) | Like `call_llm` but accepts arrays/scalars when `allow_scalar=True`; returns `Any` |
| `request_chat_completion_content` | [libs/jarvis_common/jarvis_common/llm_client.py:79-127](../../libs/jarvis_common/jarvis_common/llm_client.py#L79-L127) | Raw chat completion; returns string content with think-blocks stripped |
| `embed_texts` | [libs/jarvis_common/jarvis_common/llm_client.py:201-238](../../libs/jarvis_common/jarvis_common/llm_client.py#L201-L238) | Embeddings request via LiteLLM `/v1/embeddings`; returns vectors in input order |
| `ChatCompletionOptions` | [libs/jarvis_common/jarvis_common/llm_client.py:33-48](../../libs/jarvis_common/jarvis_common/llm_client.py#L33-L48) | Frozen dataclass with model, max_tokens, temperature, timeout, response_format, system |
| `stage2_llm_rerank` | [services/paper_ingestion/paper_ingestion/pulse/scoring.py:225-334](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L225-L334) | Bounded-concurrency LLM scoring per Pulse candidate; calls `call_llm` with `{"type":"json_object"}` and parses `relevance`/`novelty`/`reasoning` |
| `build_scoring_prompt` | [services/paper_ingestion/paper_ingestion/pulse/prompts.py:36-136](../../services/paper_ingestion/paper_ingestion/pulse/prompts.py#L36-L136) | Builds two-message chat list for Pulse stage-2 scoring |
| `extract_fields_for_paper` | [services/paper_ingestion/paper_ingestion/extraction/core.py:86-273](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L86-L273) | Template-driven LLM extraction with QuoteVerifier-gated value persistence |
| `extract_entities_for_paper` | [services/paper_ingestion/paper_ingestion/extraction/entities.py:255-464](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L255-L464) | KG entity + relationship extraction; QuoteVerifier-gated edge persistence |
| `CardGenerator.generate_cards` | [services/learning_engine/learning_engine/card_generator.py:265-317](../../services/learning_engine/learning_engine/card_generator.py#L265-L317) | Flashcard generation with quote-verification rules 5/6/7 |
| `_classify_candidate` | [services/paper_ingestion/paper_ingestion/services/contradictions.py:510-532](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L510-L532) | LLM contradiction classifier; returns parsed dict or None |
| `scan_contradictions` | [services/paper_ingestion/paper_ingestion/services/contradictions.py:535-597](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L535-L597) | Outer scan loop; trace boundary candidate |
| `generate_weekly_summary` | [services/paper_ingestion/paper_ingestion/weekly_summary.py:63-259](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L63-L259) | Per-topic LLM digest with theme-quote verification |
| `decompose_query` | [services/paper_ingestion/paper_ingestion/rag/decomposition.py:22-88](../../services/paper_ingestion/paper_ingestion/rag/decomposition.py#L22-L88) | RAG sub-query decomposition via `call_llm_json_value` (scalar list[str]) |
| `ExtractedField` | [services/paper_ingestion/paper_ingestion/models/extractions.py:81-89](../../services/paper_ingestion/paper_ingestion/models/extractions.py#L81-L89) | Existing storage model: value/quote/verified/confidence/chunk_id/page_number |
| `ExtractionField` (template field def) | [services/paper_ingestion/paper_ingestion/models/extractions.py:40-46](../../services/paper_ingestion/paper_ingestion/models/extractions.py#L40-L46) | name/label/description/type — needs name regex validator (§4.2) |
| `EntityExtractionResponse` | [services/paper_ingestion/paper_ingestion/models/kg.py:115-124](../../services/paper_ingestion/paper_ingestion/models/kg.py#L115-L124) | Existing return shape for entity extraction; unchanged by this sprint |
| `PULSE_SCORING_SYSTEM_PROMPT` | [services/paper_ingestion/paper_ingestion/pulse/prompts.py:18-33](../../services/paper_ingestion/paper_ingestion/pulse/prompts.py#L18-L33) | System message asking model to return strict JSON |
| `DIGEST_PROMPT` | [services/paper_ingestion/paper_ingestion/weekly_summary.py:36-60](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L36-L60) | Per-topic digest user-message template |
| `CARD_GENERATION_PROMPT` | [services/learning_engine/learning_engine/card_generator.py:32-61](../../services/learning_engine/learning_engine/card_generator.py#L32-L61) | Card generator user-message template |
| LiteLLM `/v1/chat/completions` endpoint | [libs/jarvis_common/jarvis_common/llm_client.py:107](../../libs/jarvis_common/jarvis_common/llm_client.py#L107) | OpenAI-compatible path; what `instructor.from_openai` will hit via `base_url=litellm.base_url` |
| ARCHITECTURE LLM-client section | [docs/ARCHITECTURE.md:34-60](../ARCHITECTURE.md#L34-L60) | Current scope: jarvis_common owns shared LLM client, prompt safety, etc. — needs update |
| ENGINEERING_STANDARDS Anti-Hallucination | [docs/ENGINEERING_STANDARDS.md:73-89](../ENGINEERING_STANDARDS.md#L73-L89) | Quote verification still required after this sprint; Instructor doesn't replace verifier |
| META Phase B.1 description | [docs/plans/2026-04-30-marathon-meta.md:130-146](../plans/2026-04-30-marathon-meta.md#L130-L146) | Source of truth for B.1 acceptance criteria |
| META Phase B.2 description | [docs/plans/2026-04-30-marathon-meta.md:148-160](../plans/2026-04-30-marathon-meta.md#L148-L160) | Source of truth for B.2 acceptance criteria |

Identifiers cited above without a row are provisional; the implementer must Read them before acting.
