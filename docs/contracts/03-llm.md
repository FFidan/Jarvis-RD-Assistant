# 03 — LLM Call Contract
**Status:** LIVING
**Reviewers must update this contract in the same patch as any change to:**
- The public surface of [libs/jarvis_common/jarvis_common/llm_client.py](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py)
- Any of the LLM call sites enumerated in §2
- The Pydantic response models in §4
- The retry / fallback policy

This contract describes the LLM call surface. All structured output flows
through Instructor-patched `call_llm_structured`; raw streaming and embeddings
are the documented exceptions (§6).

---

## 0. What this contract covers (and what it does NOT)

**In scope.**
- The single LLM choke point in `jarvis_common.llm_client`
- The structured-output call sites in services
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

`jarvis_common.llm_client` exports exactly four public functions. No code
outside this module may construct a chat-completions HTTP request directly.

| Function | File:line | Purpose | Returns |
|---|---|---|---|
| `call_llm_structured` | [llm_client.py:328](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L328) | Strict-JSON structured output via Instructor | `T` (a Pydantic `BaseModel` subclass) |
| `request_chat_completion_content` | [llm_client.py:226](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L226) | Raw chat completion | `str` (think-blocks stripped) |
| `embed_texts` | [llm_client.py:442](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L442) | Embeddings | `list[list[float]]` |
| `get_litellm_config` | [llm_client.py:123](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L123) | Resolve LiteLLM base URL | `LiteLLMConfig` |

There is no `call_llm` or `call_llm_json_value` — the older dict-returning
helpers were removed; there is no backwards-compat alias.

### 1.0 Structured-output enforcement mechanism

Every structured call is **grammar-constrained by construction**. The
Instructor client is built once per service lifespan at
[app_factory.py:467-473](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/app_factory.py#L467-L473)
with `instructor.Mode.JSON_SCHEMA`. This mode emits a native
`response_format={"type":"json_schema","json_schema":{…}}` header on every
chat-completion request — it does NOT inject the schema into the prompt text.

The schema reaches the model as a grammar constraint at the runtime layer:

- **Ollama** (`ollama_chat/` prefix → `/api/chat`): the `format:<schema>`
  field in the request body; Ollama enforces it via constrained token sampling.
- **vLLM** (`vllm/` prefix): the `guided_json` parameter; vLLM enforces the
  same guarantee.

Because the constraint is structural (grammar-enforced at the decoding layer),
a model **cannot echo the schema object instead of a schema instance** — the
output is grammatically forced to be a conforming JSON value. This is the root
fix for the v0.9.1 flagship schema-echo regression.

`call_llm_structured` itself passes **no** `response_format` argument to
Instructor — the client's Mode handles that. The `ChatCompletionOptions.response_format`
field is therefore irrelevant to structured calls (it exists for
`request_chat_completion_content` only).

**Second line of defence.** Grammar constraints enforce structure, type, and
enum membership. They do NOT enforce numeric bounds (`ge`/`le`) or string
length (`min_length`/`max_length`) — those constraints are owned by the Pydantic
field definitions and enforced at parse time by Instructor, triggering a
`ValidationError` → retry loop (up to `max_retries=2`, §3.2).

**Observability.** `SystemCapabilities` (M1.4, `GET /api/system/capabilities`)
reports a `structured_output_enforced` verdict. An admin-gated effective-config
dump endpoint (M1.5) surfaces the resolved mode; its exact shape is defined in
that task's contract.

### 1.1 `call_llm_structured` signature

```python
async def call_llm_structured(
    openai_client: "openai.AsyncOpenAI",
    *,
    response_model: type[T],          # Pydantic BaseModel subclass
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions | None = None,
    config: LiteLLMConfig | None = None,
    max_retries: int = 2,
) -> T: ...
```

It expects `openai_client` to be already instructor-patched at service startup
(built once in the service lifespan) and calls `chat.completions.create(...)`
directly — it does NOT re-wrap with `instructor.from_openai()`.

Either `prompt` (single user message) or `messages` (full chat list) is
accepted; when both are supplied, `prompt` is appended as a final user message,
prepending the `options.system` system message if set and not already present.

### 1.2 `ChatCompletionOptions`

`@dataclass(frozen=True)` at [llm_client.py:91-100](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L91-L100). Default values:
`model="smart"`, `max_tokens=2000`, `temperature=0.1`, `timeout=120.0` (`LLM_TIMEOUT_DEFAULT`), `response_format=None`, `system=None`.

`response_format` is irrelevant to `call_llm_structured` (Instructor handles
JSON-mode internally) — it remains for `request_chat_completion_content`.

---

## 2. Per-site catalog

Nine `call_llm_structured` call sites. Each site has its own row below; details in §4.

| # | Site | File:line | Model alias | Output Pydantic | QuoteVerifier? |
|---|---|---|---|---|---|
| 1 | Pulse Stage-2 reranker | [pulse/scoring.py:309](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L309) | `fast` (default; env-overridable) | `PulseScoringOutput` | Yes (post-LLM, on `reasoning`) |
| 2 | Template-driven extraction | [extraction/core.py:188](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/extraction/core.py#L188) | `get_smart_model()` | dynamic via `create_model` over `ExtractedFieldOutput` | Yes (per-field `quote`) |
| 3 | KG entity + relationship | [extraction/entities.py:146](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/extraction/entities.py#L146) | `get_fast_model()` | `KGExtractionOutput` | Yes (per-relationship `evidence`) |
| 4 | Flashcard generation | [learning_engine/card_generator.py:106](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/learning_engine/learning_engine/card_generator.py#L106) | `validated_model(model)` (default `"smart"`) | `CardGenerationOutput` | Yes (per-card `evidence_quote`) |
| 5 | Contradiction classifier | [services/contradictions.py:266](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/services/contradictions.py#L266) | `get_smart_model()` | `ContradictionClassification` | Yes (post-LLM, on `quote_a` and `quote_b`) |
| 6 | Weekly digest | [weekly_summary.py:187](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/weekly_summary.py#L187) | `get_smart_model()` | `WeeklyDigestOutput` | Optional (per-theme cheap fuzzy match against title+brief corpus) |
| 7 | Paper summarization | [services/summarization.py:207](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/services/summarization.py#L207) | `get_smart_model()` | `SummarizationOutput` (single window) / `WindowDigest` + `CondensedDigest` + `ReduceSummary` (map-reduce, §4.7) | Yes (per-finding quote verified against the window the model saw) |
| 8 | Query decomposition | [rag/decomposition.py:74](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/rag/decomposition.py#L74) | `"fast"` (default, caller-overridable) | `RootModel[list[str]]` | No (structural sub-queries; no scientific claim to verify) |
| 9 | RAG answer (`/ask`) | [routers/rag.py:132](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/routers/rag.py#L132) | resolved `smart` alias | `AskResponse` | Yes (sentence-level verifier on the answer; sources carry per-sentence confidence) |

Site 9 is the conversational-RAG answer path: `_call_rag_llm` (wrapped by
`@observe()`) is invoked by both `ask_paper` (`POST /api/papers/{paper_id}/ask`)
and `ask_cross_paper` (`POST /api/ask`) with `max_tokens=700` and
`timeout=LLM_TIMEOUT_DEFAULT`. The streaming variants of these endpoints take
the raw-streaming path in §6.1 instead.

There is also a non-call-site streaming path; it stays outside Instructor — see §6.

---

## 3. Timeout, retry, and fallback policy

### 3.1 Timeout

Per-call timeout is owned by `ChatCompletionOptions.timeout`. Three named
defaults at [llm_client.py:69-71](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L69-L71):

| Constant | Value | Used by |
|---|---|---|
| `LLM_TIMEOUT_SHORT` | 30 s | `decompose_query` (small fast prompt) |
| `LLM_TIMEOUT_DEFAULT` | 120 s | Most structured sites (incl. the RAG answer site) unless overridden |
| `LLM_TIMEOUT_LONG` | 300 s | `card_generator` (longer paper context) |

Stage-level caps are owned by callers (e.g. Pulse Stage 2's outer wall-clock cap; see
[02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy)). The choke point does NOT enforce stage-level
budgets; that's caller responsibility.

### 3.2 Retry

`call_llm_structured` defaults to `max_retries=2`. On Pydantic
`ValidationError`, Instructor re-prompts the LLM with the validation error
message included; up to 2 retry round-trips are performed before
`ValidationError` propagates to the call site.

**Retries cost up to 3× round-trip time** — caller stage budgets must account
for this. Pulse's Stage-2 wall-clock cap is the tightest constraint; Pulse
lowers its structured-output retry budget to 1 via `PULSE_STAGE2_MAX_RETRIES`
(see [02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy)).

### 3.3 Fallback per site

Every call site MUST wrap `call_llm_structured` in a `try/except` that
catches `pydantic.ValidationError` AND the related exception classes
(`ValueError`, `RuntimeError`, `httpx.HTTPError`, `KeyError`, `TypeError`).
The fallback for each site is documented inline below.

| Site | Fallback on exception |
|---|---|
| 1 Pulse Stage-2 | `ScoredCandidate` with `llm_relevance=None`, `llm_novelty=None`, `reasoning="LLM scoring failed"`. The candidate stays in the deck with Stage 1 signals only. |
| 2 Extraction | Re-raise. Caller `batch_extract` catches and increments `failed`; per-paper isolation. |
| 3 KG entity | Re-raise. Caller in `routers/` catches and returns 500 (no per-paper isolation today; see Cleanup §7). |
| 4 Card generator | The per-call helper returns `None`; caller returns `_empty_result()` (LOW confidence, zero cards). |
| 5 Contradiction classifier | Caller `scan_contradictions` catches and increments `llm_failures`; pair is skipped. |
| 6 Weekly digest | Per-topic catch; falls back to default summary text and empty themes for that topic; other topics still process. |
| 7 Paper summarization | Re-raise to the summarize job/caller, which records the failure per paper. |
| 8 Query decomposition | Caller falls back to the single original query (no sub-query expansion). |
| 9 RAG answer | Timeout maps to HTTP 504; empty visible content maps to HTTP 502 with a degraded detail object rather than a blank answer. |

Failure handling is **per-site**, not centralized. The contract requires
that no site lets a `ValidationError` propagate to a user-visible error —
either it is caught and degraded, or the surrounding job/endpoint owns the
fallback semantics.

---

## 4. Per-site Pydantic response models

All models use `from pydantic import BaseModel, Field, Literal`. Constraint
ranges are enforced by Instructor at parse time, so the call sites do not need
to re-clamp the values afterward.

### 4.1 Site 1 — Pulse Stage-2 (`PulseScoringOutput`)

```python
class PulseScoringOutput(BaseModel):
    relevance: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    reasoning: str = Field(min_length=1, max_length=400)
```

Instructor enforces the `1..10` range at parse time, so the scorer does not
re-clamp `relevance` / `novelty`.

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

The template's `ExtractionField.name` must match `^[a-zA-Z_][a-zA-Z0-9_]*$`
(the Python identifier rule, enforced on `ExtractionField.name` in
[models/extractions.py](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/models/extractions.py)).

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

The `Literal` constraint on `type` pressures the model toward the canonical
names; Instructor retries on mismatch rather than silently dropping an
out-of-vocabulary type.

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

The `Literal` on `card_type` is the authority for valid card types — there is
no separate post-LLM allow-list clamp.

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
fires and Instructor re-prompts rather than silently dropping the response.

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

### 4.7 Site 7 — Paper summarization (single-window fast path + map-reduce)

`generate_paper_summary` reads the **entire** paper regardless of length.

**Single-window fast path.** When the escaped full text fits the input char
budget (`max_input_chars(llm_smart_num_ctx, reserved_output_tokens=3500)`),
the summary is one `call_llm_structured` call returning `SummarizationOutput`
— prompt, options, and verification identical to the historical behavior.

**Map-reduce path** (text exceeds one window):

1. **Windowing** — `chunk_windows` (`jarvis_common/text_windows.py`) groups
   the `chunk_index`-ordered chunks into consecutive windows within the
   budget, preferring `## ` section boundaries. Every chunk lands in exactly
   one window; nothing is dropped or pre-truncated.
2. **Map** — one sequential digest call per window (`WindowDigest`:
   key points + at most 3 candidate findings, `reserved_output≈1200`
   tokens). **Quotes are minted only here**, where the model actually saw
   the text, and each is verified by `QuoteVerifier` against THAT window's
   chunks; unverified findings are discarded immediately.
3. **Reduce** — one call (`ReduceSummary`, `reserved_output=3500`)
   synthesizes brief/detailed/methodology/limitations from the digests.
   `ReduceSummary` and `CondensedDigest` have **no quote-bearing fields**:
   the reduce stages structurally cannot mint or repair a quote.
   Carried-over `key_findings` keep their window-verified quotes.
4. **Hierarchical condense** — when the concatenated digests exceed one
   window (or more than 12 digests would feed one reduce call), digests are
   condensed level-wise (`CondensedDigest`) until they fit, so arbitrarily
   long papers stay 100% read.

Each stage's input flows through `wrap_delimited(max_chars=…)`; the char
budget is re-read from settings before the reduce stage rather than reusing
the boot-time value.

The service returns `SummaryGenerationResult`: the stored `SummaryResponse`
plus `coverage` (1.0 on both generation paths — full text read by
construction; 0.0 only when the degraded abstract-fallback replaced the
summary text) and `passes` (window count: 1 on the fast path, 0 on the
idempotent existing-summary return).

---

## 5. Anti-hallucination integration

LLM-generated scientific content MUST remain evidence-backed per
[ENGINEERING_STANDARDS.md "Anti-Hallucination Invariants"](../ENGINEERING_STANDARDS.md#anti-hallucination-invariants). Instructor validation
catches *shape* errors but cannot catch *fabrication*. The QuoteVerifier
layer remains mandatory for sites that produce verifiable claims.

Anti-hallucination is **separate from Instructor validation**. Instructor
ensures shape; QuoteVerifier ensures grounding. Both are required.

### 5.1 Verification tier model

Four distinct verification semantics are in use. Each has its own bar and
confidence vocabulary; they must not be conflated.

**Tier 1 — Verbatim quote grounding** (`jarvis_common/verify.py`)

The shared `QuoteVerifier` runs exact substring match first, then
`rapidfuzz.fuzz.partial_ratio`. A quote passes at `FUZZY_THRESHOLD = 97`
(percent). Summary-level confidence is then computed over the pass rate:

| Pass rate | `Confidence` (StrEnum) |
|---|---|
| 100 % | `HIGH` |
| > 50 % | `MEDIUM` |
| ≤ 50 % | `LOW` |
| no findings | `NONE` |

Note the strict `> 50 %` boundary for MEDIUM — a 50 % pass rate is LOW.

Used by: Extraction (Site 2), KG entities (Site 3), Paper summarization
(Site 7). Unverified values are **dropped**, not stored with a low-confidence
flag.

**Tier 2 — RAG grounded-support** (`rag/verification.py`)

Synthesized RAG answers are paraphrases, not verbatim quotes. The verifier
splits the answer into sentences and accepts a sentence as grounded if the
shared verifier's best fuzzy score reaches `RAG_SUPPORT_FUZZY = 70` (percent)
— even when `verified=False` from the shared verifier (which uses the 97
bar). Calibrated against the live corpus: grounded synthesis scores ~75–77
against source passages while domain-plausible fabrications top out ~57,
giving comfortable margin on both sides.

Per-sentence confidence uses `RagConfidence` (StrEnum: HIGH / MEDIUM / LOW /
UNVERIFIED):

| Sentence pass rate | `RagConfidence` |
|---|---|
| 100 % | `HIGH` |
| ≥ 50 % | `MEDIUM` |
| > 0 % | `LOW` |
| 0 % (with ≥ 1 checkable sentence) | `UNVERIFIED` |
| no checkable sentences | `None` — no confidence event, no badge |

Note the `≥ 50 %` boundary for MEDIUM here — this diverges deliberately from
Tier 1's `> 50 %`. Do not "fix" either boundary without updating both modules
and this contract.

Used by: RAG answer (Site 9). Confidence stored on `AskResponse`.

**Tier 3 — Pulse reasoning score-buckets** (`pulse/verification.py`)

The Pulse Stage-2 verifier scores the LLM-generated `reasoning` sentence
against the paper title + abstract using the shared `QuoteVerifier`, then
maps the raw `partial_ratio` score to `RagConfidence` buckets. The mapping is
persisted to `pulse_cards.reasoning_confidence` (CHECK constraint
`pulse_cards_reasoning_confidence_check` in `db/init.sql`) and MUST NOT
change silently:

| Score | `RagConfidence` |
|---|---|
| ≥ 97 % | `HIGH` |
| ≥ 85 % | `MEDIUM` |
| ≥ 70 % | `LOW` |
| < 70 % or None | `UNVERIFIED` |

These three constants (97 / 85 / 70) are owned by `_score_to_confidence` in
`pulse/verification.py`. A DB migration is required if they change.

Used by: Pulse Stage-2 (Site 1). Optional — the card is retained with Stage 1
signals only when reasoning verification fails.

**Tier 4 — Weekly digest theme support**

Each LLM-generated theme sentence (with `[Paper N]` reference markers
stripped before scoring) is verified against the concatenated paper title +
`summary_brief` corpus for the topic via the shared `QuoteVerifier`'s fuzzy
scorer, judged against this tier's own support bar — themes are paraphrases,
so the 97 % verbatim bar does not apply here. This is a **display-only,
ephemeral** check — results are annotated inline on each theme dict
(`verified` bool + `verification_reason` str) and split into
`verified_themes` / `unverified_themes` for the frontend's VerificationBadge.
Nothing is persisted; themes are shown either way.

The support bar and bands for this tier are live-calibrated against the
actual title + brief corpus. The authoritative bar value and the calibration
bands are recorded in the module docstring of
`services/paper_ingestion/paper_ingestion/weekly_summary.py` — treat that
docstring as the source of truth rather than duplicating the number here.

Used by: Weekly digest (Site 6).

---

### 5.2 Confidence vocabulary summary

Two distinct StrEnum types are in use:

| Enum | Values | Modules |
|---|---|---|
| `Confidence` | `NONE / HIGH / MEDIUM / LOW` | `jarvis_common.verify` (Tier 1) |
| `RagConfidence` | `HIGH / MEDIUM / LOW / UNVERIFIED` | `rag.verification` (Tier 2), `pulse.verification` (Tier 3) |

They are not interchangeable. `NONE` (Tier 1) signals "nothing to verify";
`UNVERIFIED` (Tiers 2/3) signals "verified and failed." The MEDIUM boundary
diverges deliberately between Tier 1 (`> 50 %`) and Tier 2 (`≥ 50 %`).

---

### 5.3 Per-site verifier table

| Site | Verifier type | Path |
|---|---|---|
| 1 Pulse Stage-2 | `QuoteVerifier` (optional, Tier 3 bucket mapping) | [verification.py:verify_pulse_reasoning](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/pulse/verification.py) called at [scoring.py:303-308](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/pulse/scoring.py#L303-L308) — verifies `reasoning` against title+abstract |
| 2 Extraction | `QuoteVerifier` (mandatory, Tier 1) | [extraction/core.py:197-215](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/extraction/core.py#L197-L215) — per-field; unverified `value` is dropped |
| 3 KG entity | `QuoteVerifier` (mandatory, Tier 1) | [extraction/entities.py:399-413](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/extraction/entities.py#L399-L413) — relationships dropped if `evidence` not verifiable against full text |
| 4 Card gen | Custom fuzzy verify (`_verify_quote`) | [card_generator.py:72-79](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/learning_engine/learning_engine/card_generator.py#L72-L79) — unverified cards dropped; rule 5/6/7 confidence + abstract fallback ([card_generator.py:138-263](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/learning_engine/learning_engine/card_generator.py#L138-L263)) |
| 5 Contradiction | `QuoteVerifier` (mandatory, Tier 1) | [contradictions.py:_quotes_verify](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/services/contradictions.py#L408-L425) — both quotes verified; if either fails, contradiction NOT persisted |
| 6 Weekly digest | `QuoteVerifier` (display-only, Tier 4) | [weekly_summary.py](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/weekly_summary.py) `_theme_supported` — themes annotated with `verified` / `verification_reason`; split into `verified_themes` / `unverified_themes` (display only, not persisted) |
| 7 Summarization | `QuoteVerifier` (mandatory, Tier 1) | per-window in map-reduce path; unverified findings discarded immediately (§4.7) |
| 9 RAG answer | `verify_answer_sentences` (Tier 2) | [rag/verification.py](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/rag/verification.py) — sentence-level grounded-support at 70 %; result on `AskResponse` |

---

## 6. Streaming and scalar paths (Instructor exceptions)

### 6.1 RAG streaming

[rag/streaming.py:381](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/rag/streaming.py#L381) calls
`http_client.stream("POST", "/v1/chat/completions", json={"stream": True, ...})`
directly against the LiteLLM gateway. Streaming chat is intrinsically
non-structured — Instructor doesn't apply.

This is the **only** code outside `jarvis_common.llm_client` that constructs an
LLM HTTP request directly. The contract permits it because streaming has its
own framing (SSE token events) that Instructor cannot wrap. When the
observability profile is enabled, this path is wrapped by
`@observe(as_type="generation")` (see [04-observability.md §3](04-observability.md)).

The streaming `/ask/stream` endpoints take this path; the non-streaming
`/ask` answer goes through the structured site 9 (`AskResponse`).

### 6.2 Raw scalar helper (`request_chat_completion_content`)

`request_chat_completion_content` ([llm_client.py:226](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L226))
is the non-streaming scalar exception: it sends to LiteLLM `/v1/chat/completions`,
strips `<think>...</think>` blocks, records the served `smart` model, and raises
`EmptyVisibleLLMContentError` ([llm_client.py:74](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L74))
when the response has no visible content after stripping. It is part of the
public choke-point surface for plain-text completions that have no Pydantic shape.

The conversational-RAG `/ask` answer routes (`ask_paper`, `ask_cross_paper`)
return a structured `AskResponse` via site 9 and catch `EmptyVisibleLLMContentError`:
timeout failures map to HTTP 504, and empty-visible content maps to HTTP 502
with an explicit degraded detail object rather than a blank answer.

### 6.3 Query decomposition (`decompose_query`)

[rag/decomposition.py:74-85](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/rag/decomposition.py#L74-L85) uses `call_llm_structured` with `RootModel[list[str]]`:

```python
result = await call_llm_structured(
    openai_client, response_model=RootModel[list[str]], ...
)
sub_queries = result.root  # list[str]
```

This is structured site 8 — it is on the choke-point path, listed here only
because its output is a bare list rather than a nested model.

### 6.4 Embeddings (`embed_texts`)

[llm_client.py:442](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/llm_client.py#L442). Different endpoint (`/v1/embeddings`), different return shape (`list[list[float]]`), no JSON parsing, no retry, no Pydantic. Default timeout 60 s. Errors wrapped as `RuntimeError`. It is a separate function family from the chat-completion sites.

### 6.5 Canonical `@observe` import path

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
2. **No dict-returning LLM helpers.** `grep -rn "\bcall_llm\b\|\bcall_llm_json_value\b" services/ libs/ scripts/` returns no production hits — the older dict-returning helpers were removed in favor of `call_llm_structured`.
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
   MUST live in version-controlled source files (no external workflow nodes,
   no DB strings, no env-var prompts). Per [ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md).
8. **Structured calls are grammar-constrained.** The instructor client MUST be
   built with `Mode.JSON_SCHEMA` and all local structured calls MUST route via
   the `ollama_chat/` prefix (not `ollama/`). Schema-echo — where the model
   returns the schema definition instead of a conforming instance — is
   structurally impossible under this configuration. Verify with:
   `grep -n "Mode\." libs/jarvis_common/jarvis_common/app_factory.py` → `Mode.JSON_SCHEMA`.

---

## 8. Cleanup decisions deferred

| Item | Candidate dispositions |
|---|---|
| KG site 3 lacks per-paper isolation in failure path | (a) Adopt `extraction/core.py`-style per-paper try/except in batch endpoints; (b) Accept current "endpoint 500" behavior with documented retry guidance |
| Contradiction `quote_a`/`quote_b` model_validator strictness | (a) Keep the validator; (b) Permit empty quotes when `is_contradiction=True` and downgrade confidence |
| Streaming-path observability detail | `@observe(as_type="generation")` per stream span, not per token (see [04-observability.md](04-observability.md)) |
| Card-generator's custom `_verify_quote` vs the shared `QuoteVerifier` | (a) Keep custom (fuzzy match has different requirements); (b) Migrate to the shared verifier for anti-hallucination consistency |

---

## 9. Cross-contract references

- **[01-settings.md §2.2](01-settings.md#22-partial-keys-consulted-only-at-startup-only-on-a-non-core-endpoint-or-pushed-elsewhere-on-write)** — `llm.{smart,fast,embed}_model` and the cloud-provider keys live at the LiteLLM layer; this contract is concerned with the OpenAI-compatible HTTP path, not which underlying model the alias resolves to.
- **[02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy)** — Pulse Stage-2 owns its outer wall-clock cap; the per-call timeout (120 s) is owned here.
- **[04-observability.md §3](04-observability.md)** — each site here gets a `@observe(as_type="generation")` wrap on the choke-point function; per-site spans live on the surrounding `@observe()` boundary.
- **[docs/ENGINEERING_STANDARDS.md "Anti-Hallucination"](../ENGINEERING_STANDARDS.md#anti-hallucination-invariants)** — verifier requirements that this contract embeds in §5.

---

## 10. Verified Identifiers

| Citation | File:line | One-line behavior |
|---|---|---|
| `LiteLLMConfig` | libs/jarvis_common/jarvis_common/llm_client.py:79-88 | Frozen dataclass with `base_url` |
| `ChatCompletionOptions` | libs/jarvis_common/jarvis_common/llm_client.py:91-100 | Frozen dataclass: model/max_tokens/temperature/timeout/response_format/system |
| `LLM_TIMEOUT_SHORT/DEFAULT/LONG` | libs/jarvis_common/jarvis_common/llm_client.py:69-71 | 30 / 120 / 300 seconds |
| `EmptyVisibleLLMContentError` | libs/jarvis_common/jarvis_common/llm_client.py:74 | Raised when the scalar helper returns no visible content after `<think>` stripping |
| `get_litellm_config` | libs/jarvis_common/jarvis_common/llm_client.py:123 | Resolves LiteLLM base URL → `LiteLLMConfig` |
| `request_chat_completion_content` | libs/jarvis_common/jarvis_common/llm_client.py:226 | Raw chat completion; returns content with model reasoning tags removed |
| `call_llm_structured` | libs/jarvis_common/jarvis_common/llm_client.py:328 | Instructor-patched structured output; returns a validated `T` |
| Instructor client bootstrap (`Mode.JSON_SCHEMA`) | libs/jarvis_common/jarvis_common/app_factory.py:467-473 | `instructor.from_openai(…, mode=instructor.Mode.JSON_SCHEMA)` — grammar-constrained decoding by construction |
| `OLLAMA_PREFIXES` / `strip_ollama_prefix` / `is_local_ollama` | services/paper_ingestion/paper_ingestion/services/model_prefixes.py:13-30 | Transport prefix helpers; `ollama_chat/` routes to `/api/chat` (format constraint honored); `ollama/` routes to `/api/generate` (embedding) |
| `embed_texts` | libs/jarvis_common/jarvis_common/llm_client.py:442 | Embeddings via `/v1/embeddings`; ordered vectors |
| Site 1 `call_llm_structured` (Pulse Stage-2) | services/paper_ingestion/paper_ingestion/pulse/scoring.py:309 | Inside `_score_one`; `PulseScoringOutput` |
| Site 2 `call_llm_structured` (extraction) | services/paper_ingestion/paper_ingestion/extraction/core.py:188 | `get_smart_model()`; dynamic per-template model |
| Site 3 `call_llm_structured` (entities) | services/paper_ingestion/paper_ingestion/extraction/entities.py:146 | `get_fast_model()`; `KGExtractionOutput` |
| Site 4 `call_llm_structured` (cards) | services/learning_engine/learning_engine/card_generator.py:106 | `CardGenerationOutput` |
| Site 5 `call_llm_structured` (contradictions) | services/paper_ingestion/paper_ingestion/services/contradictions.py:266 | `ContradictionClassification` |
| Site 6 `call_llm_structured` (weekly) | services/paper_ingestion/paper_ingestion/weekly_summary.py:187 | `WeeklyDigestOutput` |
| Site 7 `call_llm_structured` (summarization) | services/paper_ingestion/paper_ingestion/services/summarization.py:207 | Shared by all summarization stages via `_call_summarize_llm`; `SummarizationOutput` / `WindowDigest` / `CondensedDigest` / `ReduceSummary` |
| `chunk_windows` | libs/jarvis_common/jarvis_common/text_windows.py:15 | Groups ordered chunks into char-budget windows; every chunk in exactly one window |
| Site 8 `decompose_query` `call_llm_structured` | services/paper_ingestion/paper_ingestion/rag/decomposition.py:74 | `RootModel[list[str]]` sub-queries |
| Site 9 RAG answer `_call_rag_llm` | services/paper_ingestion/paper_ingestion/routers/rag.py:132 | `AskResponse`; called by `ask_paper` / `ask_cross_paper` |
| `AskResponse` model | services/paper_ingestion/paper_ingestion/models/rag.py:40-47 | answer + sources + confidence + per-sentence verification |
| RAG streaming raw `client.stream` | services/paper_ingestion/paper_ingestion/rag/streaming.py:381 | The streaming exception (§6.1) |
| `ExtractionField` template-field def | services/paper_ingestion/paper_ingestion/models/extractions.py | Name regex validator |
| Card-generator `_verify_quote` fuzzy match | services/learning_engine/learning_engine/card_generator.py | Custom verifier |
| Anti-hallucination standard | docs/ENGINEERING_STANDARDS.md | Mandates evidence-backed claims |
