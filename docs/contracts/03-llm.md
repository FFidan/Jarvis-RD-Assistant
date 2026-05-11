# 03 — LLM Call Contract
**Status:** LIVING
**Date:** 2026-05-02
**Reviewers must update this contract in the same patch as any change to:**
- The public surface of [libs/jarvis_common/jarvis_common/llm_client.py](../../libs/jarvis_common/jarvis_common/llm_client.py)
- Any of the 7 LLM call sites enumerated in §2
- The Pydantic response models in §4
- The retry / fallback policy

This contract is the **evergreen counterpart** to the implementation spec at
[docs/archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md](../archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md). The spec describes the
*transition* (Wave 1 canary → Wave 2 bulk → Wave 3 cleanup); this contract
describes the *steady state* the transition produces.

While the transition is in flight, this contract describes the **target
state**. Pre-transition code that still uses `call_llm` / `call_llm_json_value`
violates the contract; the violations are the work items in the impl spec.

---

## 0. What this contract covers (and what it does NOT)

**In scope.**
- The single LLM choke point in `jarvis_common.llm_client`
- Six structured-output call sites in services
- Retry / timeout / fallback policy
- Anti-hallucination integration (QuoteVerifier)
- Streaming exceptions (the one place raw streaming is allowed)
- The embedding contract (separate function family)

**Out of scope.**
- Prompt template authorship (lives in code; not contract material — but
  prompts MUST live in version-controlled source per [ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md))
- Model-alias semantics (LiteLLM YAML — see [01-settings.md §2.2](01-settings.md#22-partial-keys-consulted-only-at-startup-only-on-a-non-core-endpoint-or-pushed-elsewhere-on-write))
- Trace boundaries / observability — see [04-observability.md](04-observability.md)

---

## 1. The choke point

After B.1 ships ([spec §2](../archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md)), `jarvis_common.llm_client` exports exactly four
functions. No code outside this module may construct a chat-completions
HTTP request directly.

| Function | Purpose | Returns |
|---|---|---|
| `call_llm_structured` | Strict-JSON structured output | `T` (a Pydantic `BaseModel` subclass) |
| `request_chat_completion_content` | Raw chat completion | `str` (think-blocks stripped) |
| `embed_texts` | Embeddings | `list[list[float]]` |
| `get_litellm_config` | Resolve LiteLLM base URL | `LiteLLMConfig` |

`call_llm` and `call_llm_json_value` (pre-B.1 functions) are **DELETED** in
the cutover commit (Wave 3). No backwards-compat alias.

### 1.1 `call_llm_structured` signature (target)

```python
async def call_llm_structured(
    http_client: httpx.AsyncClient,
    *,
    response_model: type[T],          # Pydantic BaseModel subclass
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions | None = None,
    config: LiteLLMConfig | None = None,
    max_retries: int = 2,
) -> T: ...
```

Implementation: patches `openai.AsyncOpenAI(base_url=litellm.base_url, api_key="dummy")`
with `instructor.from_openai(client, mode=instructor.Mode.JSON)`, then calls
`chat.completions.create(response_model=T, max_retries=max_retries, ...)`.

Either `prompt` (single user message) or `messages` (full chat list) is
accepted; `prompt` is sugar for `messages=[{"role":"user","content":prompt}]`
plus the `options.system` system message if set.

### 1.2 `ChatCompletionOptions` (unchanged from pre-B.1)

`@dataclass(frozen=True)` at [llm_client.py:33-48](../../libs/jarvis_common/jarvis_common/llm_client.py#L33-L48). Default values:
`model="smart"`, `max_tokens=2000`, `temperature=0.1`, `timeout=120.0`, `response_format=None`, `system=None`.

`response_format` is irrelevant to `call_llm_structured` (Instructor handles
JSON-mode internally) — it remains for `request_chat_completion_content`.

---

## 2. Per-site catalog

Seven LLM call sites. Each site has its own row below; details in §4.

| # | Site | File:line | Model alias | Output Pydantic | QuoteVerifier? |
|---|---|---|---|---|---|
| 1 | Pulse Stage-2 reranker | [pulse/scoring.py:282](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L282) | `"smart"` | `PulseScoringOutput` | Yes (post-LLM, on `reasoning`) |
| 2 | Template-driven extraction | [extraction/core.py:157](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L157) | `get_smart_model()` | dynamic via `create_model` over `ExtractedFieldOutput` | Yes (per-field `quote`) |
| 3 | KG entity + relationship | [extraction/entities.py:288](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L288) | `get_fast_model()` | `KGExtractionOutput` | Yes (per-relationship `evidence`) |
| 4 | Flashcard generation | [learning_engine/card_generator.py:119](../../services/learning_engine/learning_engine/card_generator.py#L119) | `validated_model(model)` (default `"smart"`) | `CardGenerationOutput` | Yes (per-card `evidence_quote`) |
| 5 | Contradiction classifier | [services/contradictions.py:516](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L516) | `get_smart_model()` | `ContradictionClassification` | Yes (post-LLM, on `quote_a` and `quote_b`) |
| 6 | Weekly digest | [weekly_summary.py:178](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L178) | `get_smart_model()` | `WeeklyDigestOutput` | Optional (per-theme cheap fuzzy match against title+brief corpus) |
| 7 | Paper summarization | [services/summarization.py:241](../../services/paper_ingestion/paper_ingestion/services/summarization.py#L241) | `get_smart_model()` | `SummarizationOutput` | Yes (per-finding quote verified against chunk text) |

There is also a non-call-site streaming path and a non-structured scalar
path; both stay outside Instructor — see §6.

---

## 3. Timeout, retry, and fallback policy

### 3.1 Timeout

Per-call timeout is owned by `ChatCompletionOptions.timeout`. Three named
defaults at [llm_client.py:15-17](../../libs/jarvis_common/jarvis_common/llm_client.py#L15-L17):

| Constant | Value | Used by |
|---|---|---|
| `LLM_TIMEOUT_SHORT` | 30 s | `decompose_query` (small fast prompt) |
| `LLM_TIMEOUT_DEFAULT` | 120 s | All structured sites (sites 1–6) unless overridden |
| `LLM_TIMEOUT_LONG` | 300 s | `card_generator` (longer paper context) |

Stage-level caps are owned by callers (e.g. Pulse Stage 2's 600 s cap; see
[02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy)). The choke point does NOT enforce stage-level
budgets; that's caller responsibility.

### 3.2 Retry

`call_llm_structured` defaults to `max_retries=2`. On Pydantic
`ValidationError`, Instructor re-prompts the LLM with the validation error
message included; up to 2 retry round-trips are performed before
`ValidationError` propagates to the call site.

**Retries cost up to 3× round-trip time.** Caller stage budgets must
account for this. Pulse's 600 s Stage-2 cap is the tightest constraint —
if Stage-2 retry rate measured during canary exceeds the budget, the
implementation plan reduces `max_retries` to 1 (per [spec §6.3](../archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md)).

### 3.3 Fallback per site

Every call site MUST wrap `call_llm_structured` in a `try/except` that
catches `pydantic.ValidationError` AND the legacy exception classes
(`ValueError`, `RuntimeError`, `httpx.HTTPError`, `KeyError`, `TypeError`).
The fallback for each site is documented inline below.

| Site | Fallback on exception |
|---|---|
| 1 Pulse Stage-2 | `ScoredCandidate` with `llm_relevance=None`, `llm_novelty=None`, `reasoning="LLM scoring failed"` ([scoring.py:320-331](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L320-L331)). The candidate stays in the deck with Stage 1 signals only. |
| 2 Extraction | Re-raise. Caller `batch_extract` ([extraction/core.py:299-322](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L299-L322)) catches and increments `failed`; per-paper isolation. |
| 3 KG entity | Re-raise. Caller in `routers/` catches and returns 500 (no per-paper isolation today; see Cleanup §7). |
| 4 Card generator | `_call_llm_for_cards` returns `None` ([card_generator.py:125-127](../../services/learning_engine/learning_engine/card_generator.py#L125-L127)); caller returns `_empty_result()` (LOW confidence, zero cards). |
| 5 Contradiction classifier | Caller `scan_contradictions` ([services/contradictions.py:556-567](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L556-L567)) catches and increments `llm_failures`; pair is skipped. |
| 6 Weekly digest | Per-topic catch ([weekly_summary.py:195-197](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L195-L197)); falls back to default summary text and empty themes for that topic; other topics still process. |

Failure handling is **per-site**, not centralized. The contract requires
that no site lets a `ValidationError` propagate to a user-visible error —
either it is caught and degraded, or the surrounding job/endpoint owns the
fallback semantics.

---

## 4. Per-site Pydantic response models (target)

All models use `from pydantic import BaseModel, Field, Literal`. Constraint
ranges below mirror existing post-LLM clamps in current code; once
Instructor enforces them at parse time, the manual clamps are deleted.

### 4.1 Site 1 — Pulse Stage-2 (`PulseScoringOutput`)

Lives in `services/paper_ingestion/paper_ingestion/pulse/models.py` (new file).

```python
class PulseScoringOutput(BaseModel):
    relevance: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    reasoning: str = Field(min_length=1, max_length=400)
```

The post-LLM `max(1, min(10, …))` clamps in [scoring.py:292-293](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L292-L293) become unnecessary and are deleted.

### 4.2 Site 2 — Extraction (`ExtractedFieldOutput` + dynamic per-template)

```python
class ExtractedFieldOutput(BaseModel):
    value: str | int | float | None = Field(
        description="Extracted value, or null if not in source"
    )
    quote: str | None = Field(
        default=None,
        description="Verbatim source-text quote",
    )

# At call time per template:
PaperExtractionOutput = create_model(
    f"PaperExtractionOutput_<template_id_hash>",
    **{f["name"]: (ExtractedFieldOutput | None, Field(default=None))
       for f in template_fields},
)
```

The template's `ExtractionField.name` MUST match `^[a-zA-Z_][a-zA-Z0-9_]*$`
(Python identifier rule; enforced by a new validator on
`ExtractionField.name` at [models/extractions.py:40-46](../../services/paper_ingestion/paper_ingestion/models/extractions.py#L40-L46) per spec §4.2).

### 4.3 Site 3 — KG entity extraction (`KGExtractionOutput`)

```python
class KGEntityCandidate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["method", "dataset", "metric", "concept", "institution", "author"]
    description: str | None = Field(default=None, max_length=500)

class KGRelationshipCandidate(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: Literal["used_on", "outperforms", "extends", "evaluates", "proposes", "affiliated_with"]
    evidence: str = Field(min_length=10)

class KGExtractionOutput(BaseModel):
    entities: list[KGEntityCandidate] = Field(default_factory=list, max_length=15)
    relationships: list[KGRelationshipCandidate] = Field(default_factory=list, max_length=10)
```

The Literal constraint on `type` is **stricter** than current code. Today
[entities.py:300](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L300) accepts any string and silently drops invalid `type`. After B.1, Instructor pressures the model to use the canonical names, retrying on mismatch.

### 4.4 Site 4 — Card generation (`CardGenerationOutput`)

```python
class CardOutput(BaseModel):
    card_type: Literal["concept", "quote", "method", "comparison"]
    front: str = Field(min_length=10, max_length=500)
    back: str = Field(min_length=5, max_length=2000)
    evidence_quote: str = Field(min_length=20)
    page_number: int | None = Field(default=None, ge=1)

class CardGenerationOutput(BaseModel):
    cards: list[CardOutput] = Field(min_length=1, max_length=20)
```

`VALID_CARD_TYPES = frozenset(...)` ([card_generator.py:30](../../services/learning_engine/learning_engine/card_generator.py#L30)) and the post-LLM `if card_type not in VALID_CARD_TYPES: card_type = "concept"` clamp are deleted.

### 4.5 Site 5 — Contradiction classifier (`ContradictionClassification`)

```python
class ContradictionClassification(BaseModel):
    is_contradiction: bool
    contradiction_type: Literal["direct", "methodological", "result", "interpretation"] = "direct"
    explanation: str = Field(min_length=10, max_length=400)
    quote_a: str = Field(default="")
    quote_b: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _quotes_required_if_contradiction(self) -> Self:
        if self.is_contradiction and (not self.quote_a.strip() or not self.quote_b.strip()):
            raise ValueError(
                "is_contradiction=True requires non-empty quote_a and quote_b"
            )
        return self
```

When the LLM returns `is_contradiction=True` without quotes, the validator
fires and Instructor re-prompts. Stricter than [contradictions.py:528-531](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L528-L531) which silently drops such responses.

### 4.6 Site 6 — Weekly digest (`WeeklyDigestOutput`)

```python
class ThemeOutput(BaseModel):
    theme: str = Field(min_length=10, max_length=300)
    supporting_papers: list[int] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)

class WeeklyDigestOutput(BaseModel):
    themes: list[ThemeOutput] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=20, max_length=600)
```

---

## 5. Anti-hallucination integration

LLM-generated scientific content MUST remain evidence-backed per
[ENGINEERING_STANDARDS.md "Anti-Hallucination Invariants"](../ENGINEERING_STANDARDS.md#L73-L89). Instructor validation
catches *shape* errors but cannot catch *fabrication*. The QuoteVerifier
layer remains mandatory for sites that produce verifiable claims.

| Site | Verifier type | Path |
|---|---|---|
| 1 Pulse Stage-2 | `QuoteVerifier` (optional) | [verification.py:verify_pulse_reasoning](../../services/paper_ingestion/paper_ingestion/pulse/verification.py) called at [scoring.py:303-308](../../services/paper_ingestion/paper_ingestion/pulse/scoring.py#L303-L308) — verifies `reasoning` against title+abstract |
| 2 Extraction | `QuoteVerifier` (mandatory) | [extraction/core.py:197-215](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L197-L215) — per-field; unverified `value` is dropped |
| 3 KG entity | `QuoteVerifier` (mandatory) | [extraction/entities.py:399-413](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L399-L413) — relationships dropped if `evidence` not verifiable against full text |
| 4 Card gen | Custom fuzzy verify (`_verify_quote`) | [card_generator.py:72-79](../../services/learning_engine/learning_engine/card_generator.py#L72-L79) — unverified cards dropped; rule 5/6/7 confidence + abstract fallback ([card_generator.py:138-263](../../services/learning_engine/learning_engine/card_generator.py#L138-L263)) |
| 5 Contradiction | `QuoteVerifier` (mandatory) | [contradictions.py:_quotes_verify](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L408-L425) — both quotes verified; if either fails, contradiction NOT persisted |
| 6 Weekly digest | Cheap fuzzy verifier (per-theme) | [weekly_summary.py:212-237](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L212-L237) — themes split into `verified_themes` / `unverified_themes` (display only) |

Anti-hallucination is **separate from Instructor validation**. Instructor
ensures shape; QuoteVerifier ensures grounding. Both are required.

---

## 6. Streaming and scalar paths (Instructor exceptions)

These paths do NOT use `call_llm_structured`:

### 6.1 RAG streaming

[rag/streaming.py:319-325](../../services/paper_ingestion/paper_ingestion/rag/streaming.py#L319-L325) calls
`http_client.stream("POST", "/v1/chat/completions", json={"stream": True, ...})`
directly against the LiteLLM gateway. Streaming chat is intrinsically
non-structured — Instructor doesn't apply.

This is the **only** code outside `jarvis_common.llm_client` that
constructs an LLM HTTP request directly. The contract permits it because
streaming has its own framing (SSE token events) that Instructor cannot wrap.

After [04-observability.md](04-observability.md) ships, this path is wrapped by `@observe(as_type="generation")`
boundaries; that's the only B.2 work it gets.

### 6.2 Query decomposition (`decompose_query`)

[rag/decomposition.py:22-88](../../services/paper_ingestion/paper_ingestion/rag/decomposition.py#L22-L88) uses `call_llm_json_value` (scalar list) today.
After Wave 3 of the impl spec, it migrates to:

```python
class QueryDecomposition(RootModel[list[str]]):
    pass

result = await call_llm_structured(
    http_client, response_model=QueryDecomposition, ...
)
sub_queries = result.root  # list[str]
```

This kills `call_llm_json_value` cleanly. **Until Wave 3 lands,
`call_llm_json_value` is permitted ONLY at this single call site.**

### 6.3 Embeddings (`embed_texts`)

[llm_client.py:201-238](../../libs/jarvis_common/jarvis_common/llm_client.py#L201-L238). Different endpoint (`/v1/embeddings`), different return shape (`list[list[float]]`), no JSON parsing, no retry, no Pydantic. Default timeout 60 s. Errors wrapped as `RuntimeError`.

The contract for `embed_texts` is unchanged by B.1. It remains a separate
function family.

### 6.4 Canonical `@observe` import path

All services MUST import `@observe` from `jarvis_common.llm_client`, not from
`langfuse` or `langfuse.decorators` directly. The `jarvis_common.llm_client`
module owns the three-tier import fallback (langfuse.decorators → langfuse →
no-op `functools.wraps`). Importing directly from langfuse re-introduces the
silent-no-op outage on langfuse 4.x. The unit test at
`libs/jarvis_common/tests/test_llm_client.py::test_observe_decorators_present`
asserts `__wrapped__` on every boundary function in §3 of contract 04
(observability), which would catch a regression.

---

## 7. Invariants

The implementation MUST satisfy these. Testable.

1. **Choke-point closure.** `grep -rn "POST.*v1/chat/completions\|client.stream.*chat/completions" services/ libs/ scripts/` returns matches ONLY in:
   - `libs/jarvis_common/jarvis_common/llm_client.py` (inside the choke-point)
   - `services/paper_ingestion/paper_ingestion/rag/streaming.py` (the streaming exception, §6.1)
2. **No `dict[str, Any]` LLM returns post-B.1.** `grep -rn "call_llm\b\|call_llm_json_value\b" services/ libs/ scripts/` returns no hits in production code after Wave 3 (only in test fixtures and historical docs).
3. **Every call site has a `try/except`.** Every invocation of
   `call_llm_structured` MUST be inside a `try/except` that catches at minimum
   `pydantic.ValidationError`, `ValueError`, `RuntimeError`, `httpx.HTTPError`.
4. **Anti-hallucination preserved.** Every site whose contract row in §5
   says "mandatory" MUST verify quotes before persisting any LLM-derived
   value. Verifier failure → drop the value, do not store with a low-confidence flag.
5. **Streaming exception is the ONLY exception.** No new code paths may
   bypass `call_llm_structured` for non-streaming non-embedding LLM calls.
6. **Retry budget cap.** `max_retries` MUST NOT exceed 2 without a recorded
   latency-budget review tied to a specific stage cap.
7. **Prompt provenance.** All prompt templates referenced by the call sites
   MUST live in version-controlled source files (no n8n nodes, no DB
   strings, no env-var prompts). Per [ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md).

---

## 8. Cleanup decisions deferred

| Item | Candidate dispositions |
|---|---|
| KG site 3 lacks per-paper isolation in failure path | (a) Adopt `extraction/core.py`-style per-paper try/except in batch endpoints; (b) Accept current "endpoint 500" behavior with documented retry guidance |
| `extraction/core.py` `ExtractionField.name` regex enforcement | (a) Add validator at template-create; backfill scan before B.1 cutover (mandated by spec §4.2); (b) Stay with `RootModel[dict[str, ExtractedFieldOutput]]` (less LLM steering, broader compat) |
| Contradiction `quote_a`/`quote_b` model_validator strictness | (a) Keep validator (current spec); (b) Permit empty quotes when `is_contradiction=True` and downgrade confidence; today's code already silently drops these — the validator just makes it loud |
| Streaming-path observability detail | Decided in [04-observability.md](04-observability.md) — currently planned as `@observe(as_type="generation")` per stream span, not per token |
| Card-generator's custom `_verify_quote` vs the shared `QuoteVerifier` | (a) Keep custom (fuzzy match has different requirements); (b) Migrate to shared verifier; preserves anti-hallucination consistency. Out of scope for B.1 cleanup. |

---

## 9. Cross-contract references

- **[01-settings.md §2.2](01-settings.md#22-partial-keys-consulted-only-at-startup-only-on-a-non-core-endpoint-or-pushed-elsewhere-on-write)** — `llm.{smart,fast,embed}_model` and the cloud-provider keys live at the LiteLLM layer; this contract is concerned with the OpenAI-compatible HTTP path, not which underlying model the alias resolves to.
- **[02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy)** — Pulse Stage-2 owns the 600 s wall-clock cap; per-call timeout is 120 s, owned here.
- **[04-observability.md §3](04-observability.md)** — every site here gets a `@observe(as_type="generation")` wrap on the choke-point function; per-site spans live on the surrounding `@observe()` boundary.
- **[docs/ENGINEERING_STANDARDS.md "Anti-Hallucination"](../ENGINEERING_STANDARDS.md#L73-L89)** — verifier requirements that this contract embeds in §5.
- **[docs/archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md](../archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md)** — archived implementation spec; the
  contract above describes its endpoint state.

---

## 10. Verified Identifiers

Every cited identifier was Read in the session producing this contract.

| Citation | File:line | One-line behavior |
|---|---|---|
| `LiteLLMConfig` | libs/jarvis_common/jarvis_common/llm_client.py:20-30 | Frozen dataclass with `base_url` |
| `ChatCompletionOptions` | libs/jarvis_common/jarvis_common/llm_client.py:33-48 | Frozen dataclass with model/max_tokens/temperature/timeout/response_format/system |
| `LLM_TIMEOUT_SHORT/DEFAULT/LONG` | libs/jarvis_common/jarvis_common/llm_client.py:15-17 | 30 / 120 / 300 seconds |
| `request_chat_completion_content` | libs/jarvis_common/jarvis_common/llm_client.py:79-127 | Raw chat completion; returns string |
| `call_llm` (pre-B.1) | libs/jarvis_common/jarvis_common/llm_client.py:179-198 | Strict-JSON object; returns dict — DELETED in B.1 cutover |
| `call_llm_json_value` (pre-B.1) | libs/jarvis_common/jarvis_common/llm_client.py:130-176 | Scalar/array/object JSON; returns Any — DELETED in B.1 cutover |
| `embed_texts` | libs/jarvis_common/jarvis_common/llm_client.py:201-238 | Embeddings via `/v1/embeddings`; ordered vectors |
| `LiteLLM /v1/chat/completions endpoint` | libs/jarvis_common/jarvis_common/llm_client.py:107 | OpenAI-compatible path; what `instructor.from_openai` will hit |
| Site 1 `call_llm` invocation (Pulse Stage-2) | services/paper_ingestion/paper_ingestion/pulse/scoring.py:282 | Inside `_score_one`; uses `response_format={"type":"json_object"}` |
| Site 2 `call_llm` invocation (extraction) | services/paper_ingestion/paper_ingestion/extraction/core.py:157 | Inside `extract_fields_for_paper`; uses `get_smart_model()` |
| Site 3 `call_llm` invocation (entities) | services/paper_ingestion/paper_ingestion/extraction/entities.py:288 | Inside `extract_entities_for_paper`; uses `get_fast_model()` |
| Site 4 `call_llm` invocation (cards) | services/learning_engine/learning_engine/card_generator.py:119 | Inside `_call_llm_for_cards` |
| Site 5 `call_llm` invocation (contradictions) | services/paper_ingestion/paper_ingestion/services/contradictions.py:516 | Inside `_classify_candidate` |
| Site 6 `call_llm` invocation (weekly) | services/paper_ingestion/paper_ingestion/weekly_summary.py:178 | Inside per-topic loop in `generate_weekly_summary` |
| `decompose_query` `call_llm_json_value` use | services/paper_ingestion/paper_ingestion/rag/decomposition.py:61-70 | Scalar list[str]; migrates to RootModel in Wave 3 |
| RAG streaming raw `client.stream` | services/paper_ingestion/paper_ingestion/rag/streaming.py:319-325 | The streaming exception (§6.1) |
| `ExtractedField` storage model | services/paper_ingestion/paper_ingestion/models/extractions.py:81-89 | Existing storage shape; unchanged by B.1 |
| `ExtractionField` template-field def | services/paper_ingestion/paper_ingestion/models/extractions.py:40-46 | Needs name regex validator added |
| `EntityExtractionResponse` storage | services/paper_ingestion/paper_ingestion/models/kg.py:115-124 | Existing return shape; unchanged |
| `VALID_CARD_TYPES` frozenset | services/learning_engine/learning_engine/card_generator.py:30 | Replaced by `Literal[...]` post-B.1 |
| Card-generator `_verify_quote` fuzzy match | services/learning_engine/learning_engine/card_generator.py:72-79 | Custom verifier; preserved |
| Pulse `verify_pulse_reasoning` | services/paper_ingestion/paper_ingestion/pulse/verification.py | QuoteVerifier-backed reasoning check |
| Anti-hallucination spec | docs/ENGINEERING_STANDARDS.md:73-89 | Mandates evidence-backed claims |
| Existing impl spec | docs/archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md | Drives the transition to the steady state described above |
