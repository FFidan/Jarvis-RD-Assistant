# 04 — Observability Contract
**Status:** LIVING

> **Optional — default OFF.** Langfuse observability is opt-in. The
> `observability` compose profile is not started and `OBSERVABILITY_ENABLED`
> defaults to `false`, so the SDK is never constructed (zero overhead) unless
> you explicitly run `make observability-up`. The full enablement,
> trust-boundary, and key-rotation runbook lives in
> [docs/DEPLOYMENT.md](../DEPLOYMENT.md#observability-optional-off-by-default);
> §9 here summarizes the contract-relevant invariants only.

**Reviewers must update this contract in the same patch as any change to:**
- The `@observe()` decorator placements documented in §3
- Span metadata fields (§4)
- Privacy rules (§5)
- The Langfuse SDK initialization in `configure_lifespan`

This contract describes the optional, vendor-neutral telemetry surface. Langfuse
4 remains the operator-owned LLM trace sink; optional OTLP traces and Vector
log aggregation use the same opt-in profile and never affect product work.

---

## 0. What this contract covers (and what it does NOT)

**In scope.**
- Trace boundary policy (what is one trace?)
- Span types (default control-flow vs LLM-generation)
- Standard span metadata
- Privacy / PII rules
- Sampling and opt-in posture
- SDK initialization
- Settings UI integration

**Out of scope.**
- Cost dashboards, alerting, retention policy — Langfuse-side configuration
- Frontend application telemetry (analytics, error reporting) — separate concern

---

## 1. Goals

Without Langfuse, per-LLM-call latency and cost are not directly measurable.
The Pulse Stage-2 wall-clock cap in [02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy) can only be diagnosed by reading the
`degraded_reason` after the fact — there is no per-call attribution.
Langfuse fills this gap by:

1. Capturing every LLM call (model, prompt, response, latency, token count)
2. Grouping calls into traces (Pulse run → RAG question → extraction batch)
3. Surfacing the data in a self-hosted dashboard the user can link to from Settings

---

## 2. Profile-gated, opt-in posture

Langfuse is **opt-in** via Docker Compose profile `observability`. Default
runs do not start the Langfuse container; production users who don't want
Langfuse running pay zero overhead. SDK initialization detects the absence
of Langfuse and degrades to no-op decorators.

```
docker compose --profile observability up -d langfuse
```

Required configuration (see `.env.example`):
- `OBSERVABILITY_ENABLED` — boot-gate; must be `true` for the SDK to initialize.
- `LANGFUSE_HOST` (e.g. `http://langfuse:3000`) — plain env var; when empty, `@observe()` decorators are no-ops at runtime.
- Keypair — **file-only** via Docker Secrets: `LANGFUSE_PUBLIC_KEY_FILE=/run/secrets/langfuse_init_pk` and `LANGFUSE_SECRET_KEY_FILE=/run/secrets/langfuse_init_sk` → resolved by `SecretsSettings.langfuse_public_key` / `langfuse_secret_key` (the `_FILE` convention; never set as plain env vars).
The application MUST start cleanly without Langfuse running.

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is optional and
`OTEL_EXPORT_TIMEOUT_MS` is bounded and positive. `LOG_FORWARD_ADDRESS` is an
optional `host:port` UDP destination. It is set only by `make observability-up`
to `vector:9000`; profile-off creates neither a forwarder nor a network socket.

---

## 3. Trace boundary policy

A "trace" is the outermost user-meaningful operation. Each entry below
gets exactly ONE `@observe()` wrap at the top-level function. Inner LLM
calls automatically nest as child spans of the active trace.

| Trace | Outer function | File:line | One trace produced when |
|---|---|---|---|
| **Pulse run** | `run_pulse` | [pulse/job.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/pulse/job.py) | Cron fires OR `pulse.generate` job dispatched |
| **RAG question (single-paper)** | `prepare_single_paper_rag` | [rag/streaming.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/rag/streaming.py) | User asks a question on a paper |
| **RAG question (cross-paper)** | `prepare_cross_paper_rag` | [rag/streaming.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/rag/streaming.py) | User asks a cross-paper question; includes `decompose_query` child span |
| **Extraction batch** | `batch_extract` | [extraction/core.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/extraction/core.py) | User triggers batch extraction over N papers |
| **Single-paper extraction** | `extract_fields_for_paper` | [extraction/core.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/extraction/core.py) | User triggers single-paper extraction OR is invoked from `batch_extract` (in which case it's a child span of the batch trace) |
| **KG entity extraction** | `extract_entities_for_paper` | [extraction/entities.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/extraction/entities.py) | User triggers entity extraction |
| **Card generation** | `CardGenerator.generate_cards` | [learning_engine/card_generator.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/learning_engine/learning_engine/card_generator.py) | User generates flashcards for a paper |
| **Weekly summary run** | `generate_weekly_summary` | [weekly_summary.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/weekly_summary.py) | Scheduled weekly digest job runs |
| **Contradiction scan** | `scan_contradictions` | [services/contradictions.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/services/contradictions.py) | User triggers a contradiction scan (single-paper or library-wide) |

**Implicit nested span:** every call to `call_llm_structured`
gets a `@observe(as_type="generation")` wrap at the choke-point function
itself, capturing model, input messages, validated output, latency.
Streaming RAG calls in [rag/streaming.py](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/services/paper_ingestion/paper_ingestion/rag/streaming.py) get their own
`@observe(as_type="generation")` wrap at the streaming call site (since
they don't go through the choke-point).

---

## 4. Span types and metadata

### 4.1 Span types

Two kinds of span. Per Langfuse SDK conventions:

| Type | Used for |
|---|---|
| `@observe()` (default) | Control-flow boundaries — the trace-root functions in §3 |
| `@observe(as_type="generation")` | LLM call sites — captures model, prompt, response, token counts |

Embeddings (`embed_texts`) get `@observe(as_type="generation")` with
`model="embed"` so retrieval cost surfaces in the dashboard alongside
chat-completion cost.

### 4.2 Standard metadata tags

Every span MUST attach the following tags where the value is non-null:

| Tag | Always required | Source |
|---|---|---|
| `user_id` | When known | Function parameter (the calling user's id). It is `None` for operations that have no owning user, such as system-global extraction templates, and is absent on traced entry points that take no such parameter |
| `paper_id` | When per-paper | The paper being processed |
| `template_id` | Extraction sites only | The template being applied |
| `deck_date` | Pulse run only | The date of the deck |
| `model` | Generation spans only | The LiteLLM alias (`smart`/`fast`/`embed`) or the resolved model name |
| `token_count` | Generation spans, when available | LiteLLM returns it in the response payload |
| `degraded_reason` | Pulse run, when set | Mirrors the `stats.degraded_reason` field |

Additional ad-hoc tags MAY be added per call site at the implementer's
discretion. The seven above are the minimum.

---

## 5. Privacy and PII rules

LLM-bound content is sensitive. The contract sets hard rules; violations
must fail review.

1. **No raw `user_config.value` for encrypted keys.** Any code that reads
   `user_config.value` for a key in `_ENCRYPTED_KEYS` ([01-settings.md §2.1](01-settings.md#21-active-keys-written-and-read-by-code-that-affects-user-visible-behavior))
   MUST NOT include the plaintext in any span attribute. Use
   `mask_secret(...)` or omit the field.
2. **Truncate long prompts.** Prompt content > 20,000 characters MUST be
   truncated (with an explicit `..._truncated` marker) before being sent
   to Langfuse. (Langfuse stores prompts as full text; very long prompts
   bloat the dashboard and storage.)
3. **Redact API keys in error stacks.** When an exception is captured to
   a span, its stack trace MUST be filtered for known secret patterns
   (`Bearer`, `x-api-key`, `Authorization`). Use a centralized scrubber
   (a dedicated scrubber utility — planned).
4. **No paper full text** beyond the chunks already in the prompt — span
   metadata MUST NOT include `paper.abstract` or `paper_chunks.content`
   beyond what was sent to the LLM in the captured prompt itself.
5. **Telegram chat IDs are PII.** `telegram.owner_chat_id` MUST NOT
   appear in span metadata. (Pulse runs in single-tenant mode; the
   chat_id is irrelevant to LLM traces.)

The privacy rules apply to **trace export** as well as on-disk Langfuse
storage. If Langfuse is hosted off-machine in the future, the same rules
preserve user data sovereignty.

---

## 6. Sampling

**Always-on for self-hosted.** No probabilistic sampling at the SDK level.
Cost is bounded by:

- The opt-in profile gate (most users don't run Langfuse at all)
- Trace volume per user (single-tenant; a few hundred LLM calls per day)
- Langfuse storage (self-hosted Postgres; volume grows linearly with use)

Implementations that consume Langfuse Cloud (not the self-hosted profile)
SHOULD apply a sampling rate appropriate to that contract, but the
JARVIS contract is self-hosted-first.

---

## 7. SDK initialization

[`configure_lifespan`](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/libs/jarvis_common/jarvis_common/app_factory.py)
installs one process-wide OpenTelemetry provider before service startup. The
provider exists in every profile so local W3C propagation and bounded RED
diagnostics remain available. Generic OTLP export is added only when the profile
and endpoint are configured, and its exporter accepts only spans from the
`jarvis_common.telemetry` instrumentation scope.

Research and Learning initialize Langfuse 4 through
`_langfuse_lifespan_hook`. Langfuse attaches its processor to the existing
provider and retains its explicit generation-span contract. Missing
configuration, quarantine, or exporter failure is non-fatal. Application
lifecycles perform a bounded provider flush but never shut down the shared
provider; the SDK owns final shutdown at process exit.

---

## 8. Settings UI integration

Settings → System → Observability shows an "Open Langfuse dashboard" link
when an admin has set the `observability.langfuse_dashboard_url` config key
(stored via the standard `/api/config` store, editable in that pane). The
link target is the configured value verbatim. Accepted values: an `https://`
URL, or an `http://localhost` / `http://127.0.0.1` URL (local-dev Langfuse);
the backend `_validate_langfuse_dashboard_url` guard rejects anything else.

When unset, admins see an inline URL input; non-admins see a plain "ask an
administrator" note. The Settings UI MUST NOT show a broken link or an
error placeholder.

The link is informational only — JARVIS does not embed Langfuse iframes,
proxy Langfuse API calls, or otherwise depend on Langfuse availability.

---

## 9. Enablement and operator posture

The operator runbook — `make observability-up`, `scripts/gen-langfuse-keys.sh`,
the headless `langfuse-init` first-boot provisioning, and the volume-wipe key
rotation procedure — lives in
[docs/DEPLOYMENT.md](../DEPLOYMENT.md#observability-optional-off-by-default).
This section records only the contract-relevant invariants that runbook must
preserve.

- **Write-once keypair.** Provisioning is create-if-absent. The keypair is the
  single source of truth, consumed file-only by JARVIS services via
  `LANGFUSE_PUBLIC_KEY_FILE` / `LANGFUSE_SECRET_KEY_FILE` (the `_FILE`
  convention in §2). The key files are gitignored and never committed with
  content. If a stale `langfuse_postgres_data` volume holds keys that differ
  from the current files, Langfuse silently rejects traces — rotation requires
  wiping that volume (there is no in-place key update path).
- **Operator-only sink.** `AUTH_DISABLE_SIGNUP=true` blocks new signups;
  Langfuse is not exposed to JARVIS end-users.

### 9.3 Trust-domain boundary

Langfuse is a **single deployment-wide operator telemetry tool**, decoupled from JARVIS user
identity:

- There are **no per-user Langfuse projects** — all traces go to the single operator project
- There is **no SSO or iframe bridging** — JARVIS users are never directed into Langfuse
- The only JARVIS-side surface is the admin-controlled `observability.langfuse_dashboard_url`
  config key documented in §8; the link is loopback-bound and no-proxy

Langfuse availability has no effect on JARVIS end-user functionality.

### 9.4 Profile-OFF behaviour

When `OBSERVABILITY_ENABLED` is unset or `false` (the default):

- The Langfuse SDK is **never constructed** — no import, no network socket, no background thread
- `@observe()` decorators remain in place but are no-ops (langfuse.decorators handles this case)
- W3C context and in-process RED counters remain active, but there is no trace
  exporter, forwarded-log socket, collector thread, or collector log noise

Profile-OFF is the factory default. Enabling observability is an explicit operator opt-in.

### 9.5 Post-enable admin step

After `make observability-up`, an admin sets the dashboard URL **once** in
Settings → Observability. The field is pre-filled with `http://localhost:3002` as a convenience
hint. This is an intentional one-time manual step — there is no auto-seed of the dashboard URL.
Auto-seeding would couple the Langfuse container's bound address into the service layer (YAGNI;
the URL is operator-specific and cannot be inferred generically).


### 9.6 Diagnostic artifact redaction

Perf and observability bundles are meant to leave the operator machine for agent or human review. Before any tarball is created, bundle builders MUST redact obvious API keys, auth headers, cookies, session files, and secret-like environment values while preserving logs, timings, failure bodies, and metadata needed to diagnose the run. A redaction manifest should be included in the bundle so reviewers can tell whether sanitisation ran.

### 9.7 Socketless logs and signals

Application services retain canonical structured JSON stdout in every profile.
With observability enabled, a bounded background UDP forwarder sends only safe
metadata to Vector: timestamp, level, logger, service, request ID, and
correlation ID. Queue overflow, DNS failure, backpressure, and collector outage
drop telemetry only; they never delay requests, jobs, backup, restore, or stdout
emission. Vector has no Docker socket, product API route,
product-table sink, or persistent log volume; its aggregate is available through
`docker compose logs vector`. Infrastructure services remain observable through
their own `docker compose logs` output.

Trace context follows W3C `traceparent`/`tracestate` through gateway, APIs,
signed internal HTTP, jobs, and outbox delivery. Signals remain low-cardinality:
request/worker RED outcomes, dependency health, and bounded pool, queue, outbox,
migration, and backup state. They do not contain prompt, user, cookie, token,
password, key, DSN, or authorization content.

---

## 10. Invariants

The implementation MUST satisfy these. Testable.

1. **Profile gate.** A vanilla `docker compose up -d` (no `--profile observability`) MUST start every JARVIS service successfully without errors related to Langfuse.
2. **Decorator coverage.** Every trace-boundary function in §3 MUST be wrapped by `@observe()` (decorator name as imported from `langfuse.decorators`).
3. **Generation span coverage.** Every call to `call_llm_structured` MUST
   produce exactly one `as_type="generation"` span when Langfuse is
   initialized (transitive via the choke-point decorator).
4. **PII scrubbing.** Every error span MUST pass through the secret
   scrubber before its stack trace is attached. (Verifier: targeted unit
   test that injects a known API-key pattern and asserts redaction.)
5. **Trace ID propagation.** Within a single trace, all child spans MUST
   share the trace ID. Langfuse SDK handles this automatically when
   decorators nest correctly — invariant exists to forbid manual
   trace-context creation that would break nesting.
6. **No Langfuse dependency in non-observability code.** Imports of
   `langfuse` MUST live at the top of files that already define a
   trace-boundary function, OR inside `app_factory.py`. No utility
   function in core code should depend on Langfuse being installed.
7. **Bundle redaction before export.** Any perf or observability tarball that may
   be copied off-box MUST run the artifact redaction step before archive
   creation and include a redaction manifest.
8. **Socketless forwarding.** The optional aggregate uses application UDP input;
   it does not mount the Docker socket or bulk-write logs to a product table.
9. **Export isolation.** Trace and log export queues, timeouts, and shutdown are
   bounded. Profile-off has no exporter or log-forwarder work.

---

## 11. Cleanup decisions deferred

| Item | Candidate dispositions |
|---|---|
| Token-cost telemetry | (a) Capture token counts in generation spans now; surface aggregate cost dashboard later (b) Add cost projection to span metadata using LiteLLM's reported usage |
| Sampling at high volume | Defer; revisit if a user reports Langfuse storage growth concerns |
| Frontend tracing | Out of scope; would require a separate Web SDK integration |
| Telegram bot LLM calls | None today (telegram_bot has no direct LLM calls) — revisit if telegram-side LLM features are added |

---

## 12. Cross-contract references

- **[01-settings.md §2.1](01-settings.md#21-active-keys-written-and-read-by-code-that-affects-user-visible-behavior)** — encrypted keys whose plaintext MUST NEVER appear in span metadata.
- **[02-pulse.md §6.1](02-pulse.md#61-degraded-vs-fatal-the-difference-that-matters)** — `degraded_reason` field that the Pulse trace span MUST mirror as a tag.
- **[03-llm.md §1](03-llm.md#1-the-choke-point)** — the choke-point function that gets the auto-`@observe(as_type="generation")` wrap.
- **[docs/DEPLOYMENT.md](../DEPLOYMENT.md#observability-optional-off-by-default)** — the operator enablement / provisioning / key-rotation runbook.

---

## 13. Verified Identifiers

| Citation | File:line | One-line behavior |
|---|---|---|
| `run_pulse` (trace root) | services/paper_ingestion/paper_ingestion/pulse/job.py | Top-level Pulse pipeline; one trace per overnight run |
| `prepare_single_paper_rag` | services/paper_ingestion/paper_ingestion/rag/streaming.py | RAG single-paper path |
| `prepare_cross_paper_rag` | services/paper_ingestion/paper_ingestion/rag/streaming.py | RAG cross-paper path; includes `decompose_query` child span |
| Streaming chat completion call | services/paper_ingestion/paper_ingestion/rag/streaming.py | Raw `httpx.stream`; gets generation-type span (not via choke-point) |
| `batch_extract` | services/paper_ingestion/paper_ingestion/extraction/core.py | Multi-paper extraction trace |
| `extract_fields_for_paper` | services/paper_ingestion/paper_ingestion/extraction/core.py | Per-paper extraction trace OR child span of batch |
| `extract_entities_for_paper` | services/paper_ingestion/paper_ingestion/extraction/entities.py | KG entity extraction trace |
| `CardGenerator.generate_cards` | services/learning_engine/learning_engine/card_generator.py | Card generation trace |
| `generate_weekly_summary` | services/paper_ingestion/paper_ingestion/weekly_summary.py | Weekly digest trace |
| `scan_contradictions` | services/paper_ingestion/paper_ingestion/services/contradictions.py | Contradiction scan trace |
| `configure_lifespan` (SDK init point) | libs/jarvis_common/jarvis_common/app_factory.py | Equal-length init/teardown lifespan builder |
| `_ENCRYPTED_KEYS` | services/paper_ingestion/paper_ingestion/services/config_metadata.py | Privacy: plaintext NEVER in span metadata |
| `mask_secret` | libs/jarvis_common/jarvis_common/crypto.py | Helper for scrubbing values before span attachment |
| `observability.langfuse_dashboard_url` validator | services/paper_ingestion/paper_ingestion/services/config_validators.py | Restricts the dashboard link to https / loopback http |
