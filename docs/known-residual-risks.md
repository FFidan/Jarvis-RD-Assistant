# Risk Register

_Last updated: 2026-07-06_

_Known residual risks and accepted operational/code-quality deferrals._

This document tracks acknowledged-but-deferred risks in JARVIS RD Assistant. Each entry states the rationale for deferring the full fix and the criteria that would reopen it. Resolved or rejected findings, plus internal CI and test-infrastructure tracking, are archived separately and are not part of the published site.

Related docs:

- [ARCHITECTURE.md](ARCHITECTURE.md) — runtime boundaries affected by residual risks.
- [SECURITY.md](SECURITY.md) — threat model and hardening checklist.

---

## OLLAMA-CVE-2026-7482 — Ollama daemon exposure posture — MONITOR

Current posture is documented in `docs/REQUIREMENTS.md`: the tested image pin is `ollama/ollama:0.31.2` via `versions.env`, the Compose fallback matches that pin, and the host port is loopback-only. Residual risk remains at the Docker-network boundary: containers attached to the `jarvis` network can reach `http://ollama:11434`, so untrusted peers must not join that network.

Reopen if `OLLAMA_IMAGE` is downgraded below the patched tested pin, the host publish changes away from `127.0.0.1`, an external shared-Ollama override lacks equivalent patch/bind controls, untrusted Docker-network peers are introduced, or a later Ollama advisory supersedes CVE-2026-7482 guidance.

---

## `paper_summaries.themes_verified` — descoped

**Context:** an earlier plan proposed a `themes_verified BOOL` column on `paper_summaries`.

**Current state:** the weekly response already carries `verified_themes` and `unverified_themes` in-memory. The persisted column adds storage without any UI consumer querying it.

**Why descoped:** no consumer demand identified. Migration would add schema complexity without enabling any new feature.

**Reopen criteria:** if a UI widget or API endpoint needs to filter summaries by `themes_verified` status — at which point design dedicated `weekly_digest_runs` / `weekly_digest_topics` tables rather than a bare boolean column.

---

## Contradiction-scan wall-clock budget

**Finding:** the cross-ref pre-filter for contradiction candidate pairs shipped; the remaining sub-item is an outer wall-clock timeout + `asyncio.gather` concurrency for the `_classify_candidate` LLM calls.

**Current state:** with the cross-ref pre-filter, the O(n²) pair space is reduced significantly (~95%) for typical library sizes. LLM calls still run sequentially.

**Why deferred:** wall-clock budgeting adds complexity (cancellation, partial-result semantics); sequential LLM calls are predictable. No observed timeout on current library sizes.

**Reopen criteria:** when a contradiction scan exceeds 60 seconds on real data, measured by adding a timer log in `scan_contradictions`.

---

## Auth hardening deferrals

### Migration live-fixture test deferred

One schema migration uses a defensive PL/pgSQL constraint-name lookup. The
live-fixture migration test covering this path is deferred to a future hardening
pass with proper ephemeral-Postgres test infra.

**Reopen criteria:** when a migration test harness with a real ephemeral Postgres instance is available.

---

## Telegram / security hardening deferrals

### In-memory bot rate limits

Accepted for single-user, single-bot LAN deployment. Distributed rate limiting (Redis-backed) is deferred until multi-bot or LAN-exposed scenarios materialize.

### CSP `style-src 'unsafe-inline'`

Nonce-based CSP requires a multi-day Vite plugin refactor (each style-injecting component must accept a nonce; some third-party libraries don't). Deferred.

### Requirements pinning discipline

Service `requirements.txt` files use `>=` floors (some with ceilings); the hashed pins enforced at install live in the per-service `constraints.txt` (`pip install --require-hashes`). A future pass could add explicit floor+ceiling ranges in `requirements.txt` itself to reduce drift further.

### `BotConfig.from_env()` sync/async split deferred

`from_env()` runs its one-shot DB bot-token read via `asyncio.run(...)`. This is correct today — `from_env()` is only called from the synchronous `main()` before `run_polling()` starts the event loop, so there is no running loop to conflict with. Splitting it into a sync `from_env()` + an `async_from_env()` was scoped as a bloat-reduction but carries no current bug; deferred out of the bugs/security pass to keep its regression surface zero.

---

## SMTP relay intentionally unset

**Current behavior:** Explicit empty-string SMTP secret values are rejected by
`SecretsSettings`, and the settings UI surfaces incomplete SMTP configuration
with an amber warning plus an in-place test-send action.

**Residual surface:** An instance with SMTP intentionally unset cannot deliver
magic-link email. Single-user installs can still use API-key login; multi-user
operators should configure and test SMTP before inviting users.

**Bootstrap-window note:** During the first-run wizard, before any admin account
exists, the SMTP test-send endpoint is unauthenticated; the test recipient is
forced to the From address so an operator can only mail themselves. Once an
admin account exists, an arbitrary test recipient is accepted.

**Watch for:** Treat it as a configuration regression if a future settings path
accepts an explicit empty SMTP secret, if a partial SMTP configuration is
reported as healthy, or if multi-user invite flow reports success without either
sending email or returning a usable fallback link.

---

## Builder stage installs `build` without `--require-hashes`

The Stage 1 `jarvis-common-builder` installs `build==1.2.2.post1` (and its
transitives) without `--require-hashes`. Stage 1 is ephemeral — only the
produced wheel is copied into Stage 2. Stage 2's `pip install --require-hashes
-r constraints.txt` covers every runtime dependency; the wheel enters via
`--no-deps`, so its transitives never trigger a fresh unverified resolution.

**Residual surface:** the wheel-build toolchain only, not the runtime image.

**Why deferred:** Stage 1 is not user-reachable; the runtime hash gate provides
the security boundary. Harden by generating a `constraints-builder.txt` with
`uv pip compile --generate-hashes` and switching the builder `RUN` to use it.

**Reopen criteria:** if Stage 1 gains user-reachable content or the build
toolchain is updated to a version with a known CVE.

---

## Container Hardening Exceptions

These document intentional deviations from the container-hardening sweep, each with an accepted-risk rationale.

- **`ollama/ollama` runs as root** (`docker-compose.yml` → `ollama` service). The upstream image requires uid 0 for GPU device-node access: `/dev/nvidia*` device nodes are owned by root and require either a privileged container or root to open. Switching to a non-root user breaks the NVIDIA device mount. No non-root upstream variant exists (confirmed 2026-05-26). `security_opt: ["no-new-privileges:true"]` is already set as a partial mitigation. Reopen when the upstream image ships a non-root GPU-capable variant.
- **vLLM user `1000:1000` write access to the HF cache** (`docker-compose.vllm.yml`). The vLLM service runs as `user: "1000:1000"` with `HF_HOME` on a named volume. If that volume was previously populated as root, the first non-root startup may fail with a permissions error; remove the volume before the first non-root run so Docker re-creates it owned by uid 1000. One-time operator action; document in the setup runbook if vLLM is promoted to production.
- **`requirements-optional.txt` floor-pins are informational only.** The hashed security boundary lives in `constraints-optional.txt`, pinned with sha256 hashes verified at install (`pip install --require-hashes`). `requirements-optional.txt` is auto-generated from `pyproject.toml` and may not be hand-edited (the `check-python-deps` pre-commit hook enforces parity).
- **Vector `docker.sock` access; `cap_drop: [ALL]` is defense-in-depth only.** The vector log shipper mounts `/var/run/docker.sock:ro`; `cap_drop: [ALL]` removes Linux capabilities but socket access is governed by uid/gid, so vector can still `docker inspect` other containers. The proper fix is structural (swap `docker.sock` for a syslog/fluent-bit forwarder, or run vector outside the docker network) — deferred as it would touch the logging architecture. Reopen when a log-routing redesign is in scope.

---

## Further known residual risks

### Cross-user isolation gate excludes RAG/search paths

**Finding:** the 52-scenario cross-user isolation release gate covers the core task/project/paper/user data paths but excludes the RAG and search endpoints (`/api/ask*`, `/api/search*`, `/api/similar`, and the generation pipeline) because those require a live Ollama and Qdrant instance.

**Current coverage:** `test_rag_contract.py` exercises these paths at unit/contract granularity with mocked backends. Ownership isolation at the HTTP boundary (auth headers, user-scoped Qdrant collections) is enforced by the same middleware that the gate exercises on the covered paths, giving reasonable indirect assurance.

**Why deferred:** a full live two-user RAG-path isolation test needs both inference and vector services healthy in CI, which adds significant environment complexity.

**Reopen criteria:** when a multi-user deployment scenario is targeted, or when the Qdrant collection-isolation logic changes.

---

### `/api/papers/process_batch` uses an underscore in the path

**Finding:** `/api/papers/process_batch` uses an underscore separator while peer routes use hyphens.

**Why deferred:** renaming would be a breaking change for existing clients. The frontend API client (`api.ts`) is deliberately frozen, so no functional benefit justifies the churn.

**Reopen criteria:** if the frontend client is regenerated or a breaking-change API version is introduced.

---

### Loosely-typed `dict[str, Any]` fields in two response models

**Finding:** `SystemModelsResponse` and `WeeklyDigestResponse.topics` use `dict[str, Any]` fields, which reduces OpenAPI schema fidelity and weakens static analysis on callers.

**Why deferred:** the shapes are stable and internally consistent; tightening requires adding new typed models with no user-visible change.

**Reopen criteria:** when the response shapes are consumed by a typed client or documentation that benefits from a precise schema.

---

### Minor un-hoisted duplications (low priority)

Two small consolidation items accepted as low-priority code-quality debt:

- `build_jobs_router(service_name=…)` accepts a `service_name` parameter that no code path currently uses. Retained to avoid touching every call-site; remove when the router is next refactored.
- One inline copy of the paper-visibility SQL predicate remains at `paper_ingestion/services/summarization.py`; the other copies were hoisted to a shared `paper_visible_sql()` helper. Hoist this last copy when that file is next edited.

**Reopen criteria:** any of the above files are touched in a refactor — opportunistic cleanup at that point.

---

### Job task-registry uses module-level globals as test injection points

**Finding:** `jarvis_common`'s job task registry exposes `_pool` and `_http_client` as module-level globals. Tests rely on these globals as injection points to substitute test doubles.

**Why deferred:** converting to a proper dependency-injection seam would require updating every test that directly mutates the globals, with no change in production behavior.

**Reopen criteria:** when the test suite for the job task registry is refactored, or when the globals cause a real isolation problem in CI.

---

### `paper_ingestion` settings re-export barrel retained for call-site stability

**Finding:** the `paper_ingestion.services.config` module acts as a re-export barrel for `get_paper_ingestion_settings`, rather than callers importing from the canonical `paper_ingestion.config` directly.

**Why retained:** removing the barrel would require updating every call site that currently patches `paper_ingestion.services.config.get_paper_ingestion_settings` in tests, and the production consumer that imports from that path. The marginal leanness gain does not justify the regression surface. An export-snapshot test guards the barrel's interface.

**Reopen criteria:** if a refactor already touches the majority of call sites, remove the barrel in the same pass.

---

### Context-coupling check — live-model leg skipped in CI

The context-coupling contract test (`services/paper_ingestion/tests/contract/test_ctx_coupling_contract.py`) calls `pytest.skip` when a live LiteLLM instance is not reachable, so the leg that verifies end-to-end `num_ctx` delivery to a running LiteLLM container does not run in CI. The fail-closed path (delivery failure must not advance the budget) and the write-path validator (out-of-bounds values rejected with HTTP 400) do not require a live LiteLLM and are covered unconditionally by the same test file.

**Reopen criteria:** when a CI environment with a live LiteLLM instance is available, remove the skip guard and promote the delivery test to a required gate.

---

### Partial-chunk papers not re-embedded by migration 0096

Migration 0096 set `chunked_at = NOW()` on any paper that already had at least one chunk row, marking it as fully processed. Papers that had only a partial chunk set (for example, due to an interrupted embedding run) were silently promoted to "done" by this one-time stamp. The migration was a deliberate tradeoff to avoid a full re-embed storm at upgrade time.

**Current behavior (HEAD):** the auto-pipeline and pdf-processing path pick up papers where `chunked_at IS NULL` (see `services/paper_ingestion/paper_ingestion/pipelines/auto_fetch.py:186` and `services/paper_ingestion/paper_ingestion/services/pdf_workflow.py:289`), so any future partial-chunk paper (for example from a new interrupted run) will be reprocessed on the next pipeline cycle.

**Remaining gap from migration 0096:** pre-existing partial-chunk papers that migration 0096 promoted to "done" have a non-NULL `chunked_at` and will not be automatically re-embedded. An optional post-release operator reconcile can identify them (heuristic: non-contiguous chunk index sequences) and reset `chunked_at = NULL` to trigger reprocessing. No migration is needed — this is a manual operational step.

**Reopen criteria:** if a significant number of degraded search results are traced to partial embeddings, run the reconcile.

---

### Restore destructive-sentinel: SIGKILL mitigation and operator-clear requirement

`scripts/restore.sh` writes a durable `.destructive` sentinel at the DB DROP boundary (line 178: `touch "$MAINTENANCE_DESTRUCTIVE"`), before any DROP or data modification. While this sentinel is present, the app returns HTTP 503 on all non-exempt routes regardless of sentinel age (see `libs/jarvis_common/jarvis_common/maintenance.py:11–16`). This means a SIGKILL mid-restore no longer leaves the stack silently serving a half-restored database.

**Operator action required to resume:** on any **clean** restore — same-host one-click, off-host inbox, or older-than-code — `restore.sh` clears both the `.maintenance` and `.destructive` sentinels automatically (an off-host restore reconciles its own secrets and role and self-restarts the app; an older-than-code restore is migrated forward by the app-factory watcher when the gate lifts). Only a restore that **fails after the destructive drop** exits still in maintenance, holding the durable `.destructive` sentinel; the operator recovers from the safety pre-backup and then clears both sentinels manually — see DEPLOYMENT.md for the runbook.

---

## Deferred low-value / high-churn cleanups

The following items were deliberately deferred as low-value or high-churn. Each is behavior-neutral debt, not a correctness or security risk.

- **`services_client` full migration (Telegram bot).** The bot routes most product-data calls through the typed `services_client`, but some call sites still query the shared Postgres directly. The module docstring was corrected ("most", not "all") rather than migrating every site — the remaining migration is a larger, separate effort.
- **`_owner_headers` promotion.** The Telegram `_owner_headers` helper stays at its current call-site location (pinned by tests); promoting it to a shared module is not worth the churn.
- **Service-layer `HTTPException` coupling.** A few service functions raise FastAPI `HTTPException` directly rather than a domain error the router translates. Left as-is; decoupling is a broad refactor.
- **`db_helpers` split.** `paper_ingestion`'s `db_helpers` mixes a few concerns; splitting it touches many importers for marginal gain.
- **`JOB_HANDLER_OWNER` / `noop.test` queue.** The job-handler-owner `Literal` typing and the `noop.test` queue entry are retained as-is; tightening them is cosmetic.
- **Redundant My-Day journal fetch.** `GET /api/my-day/journal` overlaps the nullable `journal` field already returned by the my-day-bundle; the separate fetch is kept to avoid a frontend refactor.
- **`jarvis_common` test-code wheel exclusion.** The `jarvis_common` wheel ships its `testing_*.py` modules (~25 KB). A `packages.find` exclude is ineffective for top-level modules (only a `testing_sidecars/` subpackage would drop); a correct fix means relocating six modules into a `jarvis_common/testing/` subpackage plus updating 100+ test imports — not worth the gain. Runtime-safe regardless (zero non-test runtime imports of `jarvis_common.testing*`).
- **`jobs.py` 503-vs-404 caller mapping.** The broad job-row-lookup failure now logs a WARNING; mapping transient DB errors to HTTP 503 at the callers is a separate enhancement — re-raising today would surface as an unhandled 500, so the status-code mapping is deferred.

**Intended behavior note — auth-first ordering (auth-idiom migration).** `paper_ingestion` route handlers now resolve identity via `Depends(current_user_id_strict)` (previously an imperative in-body call). Consequence: an *unauthenticated* request to a migrated endpoint now fails authentication (401) *before* request-body validation runs, whereas the old imperative order could surface a 422/400 body-validation error first. This is intended and more consistent/secure (uniform auth-first across all endpoints); behavior for authenticated callers is unchanged. The non-route `analyze._analyze_stream` generator retains an imperative resolve (it is not a route and has no rate-limiter).

---

## Deferred theming / test-hygiene items

- **Pytest non-fatal warnings.** Pytest emits non-fatal warnings (unawaited-coroutine in async mocks, `@pytest.mark.asyncio` on sync tests, `ORJSONResponse` deprecation). Cosmetic test-hygiene; full suite passes. Deferred.
- **Dark-mode destructive text contrast.** Dark-mode contrast of destructive error text (`text-destructive`) is below WCAG AA per Lighthouse. Proper fix is a theme-token adjustment affecting all destructive text; deferred to a theming pass.

## Deferred multi-admin attribution item

- **Multi-admin legacy-data attribution (single-admin deployments unaffected).** Migration 0094 re-owns NULL-`user_id` rows on `paper_extractions`/`paper_entities`/zotero `paper_notes` to the single admin ONLY on single-tenant boxes (mirrors 0092). On a multi-admin box with pre-existing data: (a) historical Zotero notes keep their original `discovered_by` attribution (only forward syncs are attributed to the syncing user); (b) the entity batch-backfill (`knowledge_graph.py`) attributes to `papers.discovered_by`, which for system papers (`discovered_by IS NULL`) writes read-invisible NULL-user entity rows — the user-triggered single-paper extract correctly stamps the requesting user. Acceptable for the single-operator deployment norm; revisit if a true multi-admin instance with legacy data is targeted.
