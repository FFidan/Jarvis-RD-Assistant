# No per-model `supports_structured_output` boolean

**Date:** 2026-06-25
**Status:** Accepted
**Deciders:** Engineering

---

## Context

In v0.9.1 the flagship Pulse run reported `llm_calls: 0` — the `fast` model (`qwen3:4b`) was
echoing the schema definition instead of a conforming JSON instance for `PulseScoringOutput`
(3-field flat model). The natural reflex was to add a `supports_structured_output: bool` field
to the model catalog and gate structured calls on it.

The asymmetry that killed that approach: the **same** `qwen3:4b` model was simultaneously
producing valid `KGExtractionOutput` (nested arrays, constrained enums, up to 25 objects) on
the entity-extraction path without errors. A capability boolean that reflected the Pulse failure
would have falsely blocked a working path — and one that reflected the entity success would have
missed the real failure. The discriminating variable was not the model; it was the instructor mode:
`KGExtractionOutput` ran under `Mode.JSON_SCHEMA` (grammar-constrained decoding); `PulseScoringOutput`
ran under the old `Mode.JSON` (schema injected as prompt text only), which allowed the model to
echo the schema object as output.

---

## Decision

Root-cause enforcement at the choke point via `Mode.JSON_SCHEMA` + `ollama_chat/` transport,
rather than a per-model capability boolean, a boot fail-fast, or an empirical canary per model.

The instructor client is built once per service lifespan with `instructor.Mode.JSON_SCHEMA`
([app_factory.py:467-473](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/libs/jarvis_common/jarvis_common/app_factory.py#L467-L473)).
All chat-based local aliases route via the `ollama_chat/` prefix, which carries the schema as the
`format` field to Ollama's `/api/chat` endpoint (grammar-constrained token sampling). Schema-echo
is structurally impossible: the decoding layer must produce a JSON value that conforms to the
schema, independent of what the model's internal representations prefer.

We explicitly decided **NOT** to add `supports_structured_output` to the model catalog because:

1. Capability is per (model × schema × prompt × instructor-mode), not per model.
2. A boolean changes over time with firmware/runtime updates and is a maintenance liability.
3. The correct fix is structural (enforce at the decoding layer), not a workaround gate.

---

## Evidence

| Fact | Source |
|---|---|
| Same `qwen3:4b` echoed `PulseScoringOutput` under `Mode.JSON`, produced valid `KGExtractionOutput` under `Mode.JSON_SCHEMA` | v0.9.1 live regression + root-cause analysis |
| `KGExtractionOutput` schema: nested arrays, constrained Literal enums, `min_length`, up to 25 objects | [kg_models.py:58-70](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/extraction/kg_models.py#L58-L70) |
| `PulseScoringOutput` schema: 3 flat fields (`relevance`, `novelty`, `reasoning`) | [pulse/models.py:6-11](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/pulse/models.py#L6-L11) |
| `Mode.JSON_SCHEMA` emits native `response_format: {type: json_schema, …}` — no prompt injection | instructor 1.15.1 source (`providers/openai/utils.py`) |
| `ollama_chat/` prefix → Ollama `/api/chat`, where `format:<schema>` enforces grammar constraints | [model_prefixes.py:13](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/services/model_prefixes.py#L13) |
| VRAM-tiered empirical bench data | [config/llm-tier-candidates.yaml](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/config/llm-tier-candidates.yaml) |

---

## Consequences

- **No `supports_structured_output` field.** The model catalog (`model_catalog.json`) does not
  carry this field and must not grow one — `docs/contracts/05-models-and-hardware.md §6.7` records
  this as a non-goal.
- **Capability is empirical.** A model that fails structured output today may pass tomorrow (or
  on a different schema) — the empirical grain lives in `config/llm-tier-candidates.yaml` as an
  operator-facing overlay, not as a per-catalog-entry boolean.
- **Degrade is the default.** When a structured call fails (Pydantic `ValidationError` after
  retries), each site degrades gracefully per the fallback table in
  [03-llm.md §3.3](03-llm.md#33-fallback-per-site). Pulse logs a warning and degrades to Stage 1
  ranking ([pulse/job.py:337-345](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/services/paper_ingestion/paper_ingestion/pulse/job.py#L337-L345)).
- **Opt-in hard-gate via `JARVIS_STRICT_MODELS`.** Operators who cannot tolerate graceful
  degradation set this flag; a real probe runs at startup and hard-blocks the affected feature on
  failure. This is not a boot fail-fast — the service starts; only the affected structured-output
  feature is blocked.
- **State reporting.** `SystemCapabilities.structured_output_enforced` (M1.4,
  `GET /api/system/capabilities`) reports whether `Mode.JSON_SCHEMA` is active, giving operators
  a verifiable signal without requiring a per-model probe.
