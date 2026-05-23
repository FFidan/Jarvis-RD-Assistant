# 04 — Observability Contract
**Status:** LIVING (headless operator integration shipped; see §9)
**Date:** 2026-05-02

> **Optional — default OFF.** Langfuse observability is opt-in. The
> `observability` compose profile is not started and `OBSERVABILITY_ENABLED`
> defaults to `false`, so the SDK is never constructed (zero overhead) unless
> you explicitly run `make observability-up`. Enablement, trust boundary, and
> keypair rotation are in §9.

**Reviewers must update this contract in the same patch as any change to:**
- The `@observe()` decorator placements documented in §3
- Span metadata fields (§4)
- Privacy rules (§5)
- The Langfuse SDK initialization in `configure_lifespan`

This contract is the **evergreen counterpart** to the B.2 portion of
[docs/archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md](../archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md). The spec describes
the integration work; this contract describes the steady state.

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
- Server logs (`logger.info` / `logger.warning`) — not Langfuse traces;
  contracts for those live in [ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md)
- Distributed tracing across services — Langfuse covers within-service flows;
  cross-service trace propagation is not in scope today

---

## 1. Goals

Per-LLM-call latency and cost are not measurable today. The Pulse 600 s
timeout shown in [02-pulse.md §5](02-pulse.md#5-timeout-concurrency-and-budget-policy) was diagnosed only by reading the
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

---

## 3. Trace boundary policy

A "trace" is the outermost user-meaningful operation. Each entry below
gets exactly ONE `@observe()` wrap at the top-level function. Inner LLM
calls automatically nest as child spans of the active trace.

| Trace | Outer function | File:line (target) | One trace produced when |
|---|---|---|---|
| **Pulse run** | `run_pulse` | [pulse/job.py:68](../../services/paper_ingestion/paper_ingestion/pulse/job.py#L68) | Cron fires OR `pulse.generate` job dispatched |
| **RAG question (single-paper)** | `prepare_single_paper_rag` | [rag/streaming.py:83](../../services/paper_ingestion/paper_ingestion/rag/streaming.py#L83) | User asks a question on a paper |
| **RAG question (cross-paper)** | `prepare_cross_paper_rag` | [rag/streaming.py:145](../../services/paper_ingestion/paper_ingestion/rag/streaming.py#L145) | User asks a cross-paper question; includes `decompose_query` child span |
| **Extraction batch** | `batch_extract` | [extraction/core.py:276](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L276) | User triggers batch extraction over N papers |
| **Single-paper extraction** | `extract_fields_for_paper` | [extraction/core.py:86](../../services/paper_ingestion/paper_ingestion/extraction/core.py#L86) | User triggers single-paper extraction OR is invoked from `batch_extract` (in which case it's a child span of the batch trace) |
| **KG entity extraction** | `extract_entities_for_paper` | [extraction/entities.py:255](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L255) | User triggers entity extraction |
| **Card generation** | `CardGenerator.generate_cards` | [learning_engine/card_generator.py:265](../../services/learning_engine/learning_engine/card_generator.py#L265) | User generates flashcards for a paper |
| **Weekly summary run** | `generate_weekly_summary` | [weekly_summary.py:63](../../services/paper_ingestion/paper_ingestion/weekly_summary.py#L63) | Scheduled weekly digest job runs |
| **Contradiction scan** | `scan_contradictions` | [services/contradictions.py:535](../../services/paper_ingestion/paper_ingestion/services/contradictions.py#L535) | User triggers a contradiction scan (single-paper or library-wide) |

**Implicit nested span:** every call to `call_llm_structured` (post-B.1)
gets a `@observe(as_type="generation")` wrap at the choke-point function
itself, capturing model, input messages, validated output, latency.
Streaming RAG calls in [rag/streaming.py:319](../../services/paper_ingestion/paper_ingestion/rag/streaming.py#L319) get their own
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
| `user_id` | When known | Function parameter (currently `None` in single-tenant mode; mandatory once multi-tenant lands) |
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
   `user_config.value` for a key in `_ENCRYPTED_KEYS` ([01-settings.md §2.1](01-settings.md#21-live-keys-written-and-read-by-code-that-affects-user-visible-behavior))
   MUST NOT include the plaintext in any span attribute. Use
   `mask_secret(...)` or omit the field.
2. **Truncate long prompts.** Prompt content > 20,000 characters MUST be
   truncated (with an explicit `..._truncated` marker) before being sent
   to Langfuse. (Langfuse stores prompts as full text; very long prompts
   bloat the dashboard and storage.)
3. **Redact API keys in error stacks.** When an exception is captured to
   a span, its stack trace MUST be filtered for known secret patterns
   (`Bearer`, `x-api-key`, `Authorization`). Use a centralized scrubber
   (TBD utility — placeholder for impl plan).
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

Once per service in [`configure_lifespan`](../../libs/jarvis_common/jarvis_common/app_factory.py#L151) at startup. Roughly:

```python
# Pseudocode — mirrors _langfuse_lifespan_hook in jarvis_common/llm_client.py
def _langfuse_lifespan_hook() -> None:
    settings = get_jarvis_common_settings()
    if not settings.observability_enabled:
        return  # OBSERVABILITY_ENABLED is false; @observe() decorators are no-ops
    secrets = get_secrets_settings()  # resolves LANGFUSE_PUBLIC_KEY_FILE / _SECRET_KEY_FILE
    host = settings.langfuse_host
    pk = secrets.langfuse_public_key.get_secret_value() if secrets.langfuse_public_key else None
    sk = secrets.langfuse_secret_key.get_secret_value() if secrets.langfuse_secret_key else None
    if not (host and pk and sk):
        logger.warning("OBSERVABILITY_ENABLED set but host/keys missing; traces no-op")
        return
    from langfuse import Langfuse
    Langfuse(host=host, public_key=pk, secret_key=sk)
    # never raises — broad except guards startup
```

Initialization MUST be idempotent and MUST NOT raise if configuration is
missing. The `@observe()` decorator from `langfuse.decorators` handles
the no-op case automatically when the SDK is uninitialized.

The lifespan teardown counterpart (per the equal-length contract enforced by
[configure_lifespan](../../libs/jarvis_common/jarvis_common/app_factory.py#L151)) is `langfuse.shutdown()` — flushes pending spans
to the backend, important for short-lived workers.

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

## 9. Headless provisioning

Running `make observability-up` bootstraps the full observability stack in one step:

```
scripts/gen-langfuse-keys.sh          # generate secrets/langfuse_init_pk.txt + langfuse_init_sk.txt (once)
OBSERVABILITY_ENABLED=true LANGFUSE_HOST=http://langfuse:3000 \
  docker compose --profile observability up -d langfuse paper_ingestion learning_engine
```

### 9.1 What is provisioned on first boot

When the `langfuse-init` container runs against a fresh `langfuse_postgres_data` volume it creates:

- A Langfuse **organisation** and **project**
- An **operator user** (credentials from `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`)
- A **write-once project keypair** read from `secrets/langfuse_init_pk.txt` (public key) and
  `secrets/langfuse_init_sk.txt` (secret key)

The keypair files are generated locally by `scripts/gen-langfuse-keys.sh` and are **never committed
with content** (`.gitignore` covers them). They are the **single source of truth**, consumed two
ways from the same files (no duplication into `.env`):

- **JARVIS services** read them file-only via `LANGFUSE_PUBLIC_KEY_FILE` / `LANGFUSE_SECRET_KEY_FILE`
  → `/run/secrets/…` → `SecretsSettings` (their `appuser` can read the mounted secret).
- **The Langfuse container** runs as non-root `nextjs`; Docker-Compose `file:` secrets preserve the
  host file's `0600`/uid-1000 perms, so an in-container secret read is unreadable there. Langfuse's
  headless-init contract is env-only, so `make observability-up` reads the same two files and injects
  them as `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `LANGFUSE_INIT_PROJECT_SECRET_KEY` for that container
  only (invocation-scoped, never persisted to `.env`). The image's native entrypoint runs unmodified.

`AUTH_DISABLE_SIGNUP=true` is set in the Langfuse container environment, so all new signups
(including invited users) are blocked. Langfuse is an operator-only telemetry sink; it is not
exposed to JARVIS end-users.

### 9.2 Key rotation procedure

Provisioning is **create-if-absent / write-once**. Langfuse does not update an existing project's
keys via the init container; if the `langfuse_postgres_data` volume already contains a project
whose keys differ from the current secret files, traces will be silently rejected (no error logged
by Langfuse, no trace export).

**Rotation procedure — required when the keypair must change:**

1. `docker compose --profile observability down`
2. `docker volume rm <project>_langfuse_postgres_data`
3. Delete `secrets/langfuse_init_pk.txt` and `secrets/langfuse_init_sk.txt`
4. Run `scripts/gen-langfuse-keys.sh` to generate a new keypair
5. Run `make observability-up`

There is no in-place key update path. Wiping the volume is the documented and only supported
rotation mechanism.

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
- There is **no latency delta, no log noise, no trace export**

Profile-OFF is the factory default. Enabling observability is an explicit operator opt-in.

### 9.5 Post-enable admin step

After `make observability-up`, an admin sets the dashboard URL **once** in
Settings → Observability. The field is pre-filled with `http://localhost:3002` as a convenience
hint. This is an intentional one-time manual step — there is no auto-seed of the dashboard URL.
Auto-seeding would couple the Langfuse container's bound address into the service layer (YAGNI;
the URL is operator-specific and cannot be inferred generically).


### 9.6 Diagnostic artifact redaction

Perf and observability bundles are meant to leave the operator machine for agent or human review. Before any tarball is created, bundle builders MUST redact obvious API keys, auth headers, cookies, session files, and secret-like environment values while preserving logs, timings, failure bodies, and metadata needed to diagnose the run. A redaction manifest should be included in the bundle so reviewers can tell whether sanitisation ran.

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

---

## 11. Cleanup decisions deferred

| Item | Candidate dispositions |
|---|---|
| Token-cost telemetry | (a) Capture token counts in generation spans now; surface aggregate cost dashboard later (b) Add cost projection to span metadata using LiteLLM's reported usage |
| Cross-service trace propagation | Out of scope today; would require trace ID forwarding through the jobs subsystem and via HTTP headers between paper_ingestion ↔ learning_engine |
| Sampling at high volume | Defer; revisit if a user reports Langfuse storage growth concerns |
| Frontend tracing | Out of scope; would require a separate Web SDK integration |
| Telegram bot LLM calls | None today (telegram_bot has no direct LLM calls) — revisit if telegram-side LLM features are added |

---

## 12. Cross-contract references

- **[01-settings.md §2.1](01-settings.md#21-live-keys-written-and-read-by-code-that-affects-user-visible-behavior)** — encrypted keys whose plaintext MUST NEVER appear in span metadata.
- **[02-pulse.md §6.1](02-pulse.md#61-degraded-vs-fatal-the-difference-that-matters)** — `degraded_reason` field that the Pulse trace span MUST mirror as a tag.
- **[03-llm.md §1.1](03-llm.md#11-call_llm_structured-signature-target)** — the choke-point function that gets the auto-`@observe(as_type="generation")` wrap.
- **[docs/archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md §2 / §5](../archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md)** — archived implementation spec.

---

## 13. Verified Identifiers

Every cited identifier was Read in the session producing this contract.
Several rows below are **target lines** — the function exists today, the
`@observe()` wrap will be added during B.2 implementation. The contract
author re-Reads each cited file before final claim.

| Citation | File:line | One-line behavior (post-B.2) |
|---|---|---|
| `run_pulse` (target trace root) | services/paper_ingestion/paper_ingestion/pulse/job.py:68 | Top-level Pulse pipeline; one trace per overnight run |
| `prepare_single_paper_rag` + `stream_rag_events` | services/paper_ingestion/paper_ingestion/rag/streaming.py:83, 306 | RAG single-paper path; trace covers prep + stream |
| `prepare_cross_paper_rag` | services/paper_ingestion/paper_ingestion/rag/streaming.py:145 | RAG cross-paper path; includes `decompose_query` child span |
| Streaming chat completion call | services/paper_ingestion/paper_ingestion/rag/streaming.py:319-325 | Raw `httpx.stream`; gets generation-type span (not via choke-point) |
| `batch_extract` | services/paper_ingestion/paper_ingestion/extraction/core.py:276 | Multi-paper extraction trace |
| `extract_fields_for_paper` | services/paper_ingestion/paper_ingestion/extraction/core.py:86 | Per-paper extraction trace OR child span of batch |
| `extract_entities_for_paper` | services/paper_ingestion/paper_ingestion/extraction/entities.py:255 | KG entity extraction trace |
| `CardGenerator.generate_cards` | services/learning_engine/learning_engine/card_generator.py:265 | Card generation trace |
| `generate_weekly_summary` | services/paper_ingestion/paper_ingestion/weekly_summary.py:63 | Weekly digest trace |
| `scan_contradictions` | services/paper_ingestion/paper_ingestion/services/contradictions.py:535 | Contradiction scan trace |
| `configure_lifespan` (SDK init point) | libs/jarvis_common/jarvis_common/app_factory.py:151 | Equal-length init/teardown lifespan builder |
| `_ENCRYPTED_KEYS` | services/paper_ingestion/paper_ingestion/services/settings_service.py:259-269 | Privacy: plaintext NEVER in span metadata |
| `mask_secret` | libs/jarvis_common/jarvis_common/crypto.py | Helper for scrubbing values before span attachment |
| Existing impl spec | docs/archive/2026-05/specs/2026-05-02-instructor-langfuse-integration.md | Drives the integration work that produces this contract's steady state |
