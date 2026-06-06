# Known Residual Risks

_Last updated: 2026-06-02_

This document tracks acknowledged-but-deferred risks in JARVIS RD Assistant. Each entry states the rationale for deferring the full fix and the criteria that would reopen it. Closed and falsified findings, plus internal CI/test-infra tracking, are archived separately and are not part of the published site.

Related docs:

- [ARCHITECTURE.md](ARCHITECTURE.md) — runtime boundaries affected by residual risks.
- [SECURITY.md](SECURITY.md) — threat model and hardening checklist.

---

## OLLAMA-CVE-2026-7482 — Ollama daemon exposure posture — MONITOR

Current posture is documented in `docs/REQUIREMENTS.md`: the tested image pin is `ollama/ollama:0.23.1` via `versions.env`, the Compose fallback matches that pin, and the host port is loopback-only. Residual risk remains at the Docker-network boundary: containers attached to the `jarvis` network can reach `http://ollama:11434`, so untrusted peers must not join that network.

Reopen if `OLLAMA_IMAGE` is downgraded below the patched tested pin, the host publish changes away from `127.0.0.1`, an external shared-Ollama override lacks equivalent patch/bind controls, untrusted Docker-network peers are introduced, or a later Ollama advisory supersedes CVE-2026-7482 guidance.

---

## PI-EDGE-010 — Per-tick NOTIFY listener creation

**Finding:** `_wait_for_job_notification` creates a new `asyncpg-listen` listener per SSE poll tick.

**Current state:** a 60-second job with 2-second polls generates 30 connection cycles per stream. No correctness impact; only a connection-churn smell.

**Why deferred:** no observed connection exhaustion at current job volumes (single-user). Fix requires refactoring the SSE stream to hold a single long-lived listener per job stream — moderate blast radius.

**Reopen criteria:** when job volume exceeds ~50 concurrent streams, or when connection pool exhaustion is observed in logs.

---

## Conversational-agent spike — build-vs-adopt unresolved

**Context:** the roadmap targets a conversational agent layer (Hermes).

**Current state:** not started. The build-vs-adopt decision — adopt an existing open agent framework vs. build natively on LiteLLM tool-calling plus the existing prompt harness — requires explicit sign-off before the spike is worth running. It is sequenced behind the cross-service auth work, the first performance phase, and an evaluation harness; see the [ROADMAP](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/ROADMAP.md).

**Reopen criteria:** when the roadmap prerequisites land and the build-vs-adopt path is selected.

---

## `paper_summaries.themes_verified` — descoped

**Context:** an earlier plan proposed a `themes_verified BOOL` column on `paper_summaries`.

**Current state:** the weekly response already carries `verified_themes` and `unverified_themes` in-memory. The persisted column adds storage without any UI consumer querying it.

**Why descoped:** no consumer demand identified. Migration would add schema complexity without enabling any new feature.

**Reopen criteria:** if a UI widget or API endpoint needs to filter summaries by `themes_verified` status — at which point design dedicated `weekly_digest_runs` / `weekly_digest_topics` tables rather than a bare boolean column.

---

## PI-EDGE-005 — contradiction-scan wall-clock budget

**Finding:** the cross-ref pre-filter for contradiction candidate pairs shipped; the remaining sub-item is an outer wall-clock timeout + `asyncio.gather` concurrency for the `_classify_candidate` LLM calls.

**Current state:** with the cross-ref pre-filter, the O(n²) pair space is reduced significantly (~95%) for typical library sizes. LLM calls still run sequentially.

**Why deferred:** wall-clock budgeting adds complexity (cancellation, partial-result semantics); sequential LLM calls are predictable. No observed timeout on current library sizes.

**Reopen criteria:** when a contradiction scan exceeds 60 seconds on real data, measured by adding a timer log in `scan_contradictions`.

---

## Auth hardening deferrals

### H5 — Migration live-fixture test deferred

One schema migration uses a defensive PL/pgSQL constraint-name lookup. The
live-fixture migration test covering this path is deferred to a future hardening
pass with proper ephemeral-Postgres test infra.

**Reopen criteria:** when a migration test harness with a real ephemeral Postgres instance is available.

---

## Telegram / security hardening deferrals

### TG-004 — In-memory bot rate limits

Accepted for single-user, single-bot LAN deployment. Distributed rate limiting (Redis-backed) is deferred until multi-bot or LAN-exposed scenarios materialize.

### SEC-106 — CSP `style-src 'unsafe-inline'`

Nonce-based CSP requires a multi-day Vite plugin refactor (each style-injecting component must accept a nonce; some third-party libraries don't). Deferred.

### SEC-DEP-001 — Requirements pinning discipline

Service `requirements.txt` files use `>=` floors (some with ceilings); the hashed pins enforced at install live in the per-service `constraints.txt` (`pip install --require-hashes`). A future pass could add explicit floor+ceiling ranges in `requirements.txt` itself to reduce drift further.

### Search upsert `user_id` stamping

`POST /api/search` performs external-source fetch + DB upsert via `pdf_workflow.upsert_paper`, which doesn't currently stamp `user_id` on the new row. Full multi-user end-to-end isolation requires this. Recommended follow-up.

### TG-BLOAT-01 — `BotConfig.from_env()` sync/async split deferred

`from_env()` runs its one-shot DB bot-token read via `asyncio.run(...)`. This is correct today — `from_env()` is only called from the synchronous `main()` before `run_polling()` starts the event loop, so there is no running loop to conflict with. Splitting it into a sync `from_env()` + an `async_from_env()` was scoped as a bloat-reduction but carries no current bug; deferred out of the bugs/security pass to keep its regression surface zero.

---

## SMTP-EMPTY-STRING-1 — empty-string SMTP env vars silently accepted

**Symptom:** Setting any of `SMTP_HOST`, `SMTP_FROM`, `SMTP_USER`, or `SMTP_PASS` to an empty string is silently accepted by `SecretsSettings` (no Pydantic validator rejects `""`). `_EffectiveSmtp.deliverable` evaluates `bool("") == False`, so the magic-link sender falls through to the dev-mode logging path. **The operator sees nothing at startup; users do not receive magic-link emails; the failure is silent.**

**Impact:** HIGH (operator-facing silent failure, not data loss). Affects any deployment where SMTP env vars are mis-set to `""` rather than left unset — `""` and unset have different semantics, and only unset is correctly handled.

**Mitigation:** Add a `@field_validator` to `SecretsSettings.smtp_host/from/user/pass` rejecting empty-string values, OR add a startup-time health check that asserts the SMTP configuration is internally consistent. Four `xfail(strict=False)` tests in `libs/jarvis_common/tests/test_secrets_settings.py` document the gap and will auto-green when validators are added.

---

## BUILDER-STAGE-BUILD-UNHASHED-1 — builder stage installs `build` without `--require-hashes`

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

- **C-09 — `ollama/ollama` runs as root** (`docker-compose.yml` → `ollama` service). The upstream image requires uid 0 for GPU device-node access: `/dev/nvidia*` device nodes are owned by root and require either a privileged container or root to open. Switching to a non-root user breaks the NVIDIA device mount. No non-root upstream variant exists (confirmed 2026-05-26). `security_opt: ["no-new-privileges:true"]` is already set as a partial mitigation. Reopen when the upstream image ships a non-root GPU-capable variant.
- **C-05 — vLLM user `1000:1000` write access to the HF cache** (`docker-compose.vllm.yml`). The vLLM service runs as `user: "1000:1000"` with `HF_HOME` on a named volume. If that volume was previously populated as root, the first non-root startup may fail with a permissions error; remove the volume before the first non-root run so Docker re-creates it owned by uid 1000. One-time operator action; document in the setup runbook if vLLM is promoted to production.
- **SC-09 — `requirements-optional.txt` floor-pins are informational only.** The hashed security boundary lives in `constraints-optional.txt`, pinned with sha256 hashes verified at install (`pip install --require-hashes`). `requirements-optional.txt` is auto-generated from `pyproject.toml` and may not be hand-edited (the `check-python-deps` pre-commit hook enforces parity).
- **C-06 — vector `docker.sock` access; `cap_drop: [ALL]` is defense-in-depth only.** The vector log shipper mounts `/var/run/docker.sock:ro`; `cap_drop: [ALL]` removes Linux capabilities but socket access is governed by uid/gid, so vector can still `docker inspect` other containers. The proper fix is structural (swap `docker.sock` for a syslog/fluent-bit forwarder, or run vector outside the docker network) — deferred as it would touch the logging architecture. Reopen when a log-routing redesign is in scope.

---

## Further known residual risks

### C5-3 — Cross-user isolation gate excludes RAG/search paths

**Finding:** the 52-scenario cross-user isolation release gate covers the core task/project/paper/user data paths but excludes the RAG and search endpoints (`/api/ask*`, `/api/search*`, `/api/similar`, and the generation pipeline) because those require a live Ollama and Qdrant instance.

**Current coverage:** `test_rag_contract.py` exercises these paths at unit/contract granularity with mocked backends. Ownership isolation at the HTTP boundary (auth headers, user-scoped Qdrant collections) is enforced by the same middleware that the gate exercises on the covered paths, giving reasonable indirect assurance.

**Why deferred:** a full live two-user RAG-path isolation test needs both inference and vector services healthy in CI, which adds significant environment complexity.

**Reopen criteria:** when a multi-user deployment scenario is targeted, or when the Qdrant collection-isolation logic changes.

---

### B3-006 — `/api/papers/process_batch` uses an underscore in the path

**Finding:** `/api/papers/process_batch` uses an underscore separator while peer routes use hyphens.

**Why deferred:** renaming would be a breaking change for existing clients. The frontend API client (`api.ts`) is deliberately frozen, so no functional benefit justifies the churn.

**Reopen criteria:** if the frontend client is regenerated or a breaking-change API version is introduced.

---

### B3-009 — Loosely-typed `dict[str, Any]` fields in two response models

**Finding:** `SystemModelsResponse` and `WeeklyDigestResponse.topics` use `dict[str, Any]` fields, which reduces OpenAPI schema fidelity and weakens static analysis on callers.

**Why deferred:** the shapes are stable and internally consistent; tightening requires adding new typed models with no user-visible change.

**Reopen criteria:** when the response shapes are consumed by a typed client or documentation that benefits from a precise schema.

---

### EVAL-HARNESS-1 — Retrieval/quality eval harness removed; reproducible replacement owed

**Finding:** the retrieval and pulse eval scripts and their fixtures were removed for the public release. They were not called by CI, were not user-facing, and referenced database-local paper IDs that an external contributor could not reproduce.

**Why deferred:** the scripts would have been non-runnable by anyone other than the original developer.

**Reopen criteria:** introduce a reproducible eval harness keyed on stable arXiv IDs with an accompanying seed-fetch script so a contributor can run the eval against a fresh database. Scope this as a standalone developer-tooling task.

---

### LOW-DRY-001 — Minor un-hoisted duplications (low priority)

Two small consolidation items accepted as low-priority code-quality debt:

- `build_jobs_router(service_name=…)` accepts a `service_name` parameter that no code path currently uses. Retained to avoid touching every call-site; remove when the router is next refactored.
- One inline copy of the paper-visibility SQL predicate remains at `paper_ingestion/services/summarization.py`; the other copies were hoisted to a shared `paper_visible_sql()` helper. Hoist this last copy when that file is next edited.

**Reopen criteria:** any of the above files are touched in a refactor — opportunistic cleanup at that point.

---

### JOBS-REGISTRY-GLOBALS-1 — Job task-registry uses module-level globals as test injection points

**Finding:** `jarvis_common`'s job task registry exposes `_pool` and `_http_client` as module-level globals. Tests rely on these globals as injection points to substitute test doubles.

**Why deferred:** converting to a proper dependency-injection seam would require updating every test that directly mutates the globals, with no change in production behavior.

**Reopen criteria:** when the test suite for the job task registry is refactored, or when the globals cause a real isolation problem in CI.

---

### SETTINGS-BARREL-1 — `paper_ingestion` settings re-export barrel retained for call-site stability

**Finding:** the `paper_ingestion.services.config` module acts as a re-export barrel for `get_paper_ingestion_settings`, rather than callers importing from the canonical `paper_ingestion.config` directly.

**Why retained:** removing the barrel would require updating every call site that currently patches `paper_ingestion.services.config.get_paper_ingestion_settings` in tests, and the production consumer that imports from that path. The marginal leanness gain does not justify the regression surface. An export-snapshot test guards the barrel's interface.

**Reopen criteria:** if a refactor already touches the majority of call sites, remove the barrel in the same pass.

---

## Deferred from the 2026-06-02 public-readiness fix plan

Low-value-or-high-churn items deliberately deferred during the public-readiness fix plan (PR-5). Each is behavior-neutral debt, not a correctness or security risk.

- **`services_client` full migration (Telegram bot).** The bot routes most product-data calls through the typed `services_client`, but some call sites still query the shared Postgres directly. PR-5 corrected the module docstring ("most", not "all") rather than migrating every site — the remaining migration is a larger, separate effort.
- **`_owner_headers` promotion.** The Telegram `_owner_headers` helper stays at its current call-site location (pinned by tests); promoting it to a shared module is not worth the churn.
- **Service-layer `HTTPException` coupling.** A few service functions raise FastAPI `HTTPException` directly rather than a domain error the router translates. Left as-is; decoupling is a broad refactor.
- **`db_helpers` split.** `paper_ingestion`'s `db_helpers` mixes a few concerns; splitting it touches many importers for marginal gain.
- **`JOB_HANDLER_OWNER` / `noop.test` queue.** The job-handler-owner `Literal` typing and the `noop.test` queue entry are retained as-is; tightening them is cosmetic.
- **Redundant My-Day journal fetch.** `GET /api/my-day/journal` overlaps the nullable `journal` field already returned by the my-day-bundle; the separate fetch is kept to avoid a frontend refactor.
- **`jarvis_common` test-code wheel exclusion.** The `jarvis_common` wheel ships its `testing_*.py` modules (~25 KB). A `packages.find` exclude is ineffective for top-level modules (only a `testing_sidecars/` subpackage would drop); a correct fix means relocating six modules into a `jarvis_common/testing/` subpackage plus updating 100+ test imports — not worth the gain. Runtime-safe regardless (zero non-test runtime imports of `jarvis_common.testing*`).
- **`jobs.py` 503-vs-404 caller mapping.** The broad job-row-lookup failure now logs a WARNING (observability fix shipped in PR-2); mapping transient DB errors to HTTP 503 at the callers is a separate enhancement — re-raising today would surface as an unhandled 500, so the status-code mapping is deferred.

**Intended behavior note — auth-first ordering (PR-5 auth-idiom migration).** `paper_ingestion` route handlers now resolve identity via `Depends(current_user_id_strict)` (previously an imperative in-body call). Consequence: an *unauthenticated* request to a migrated endpoint now fails authentication (401) *before* request-body validation runs, whereas the old imperative order could surface a 422/400 body-validation error first. This is intended and more consistent/secure (uniform auth-first across all endpoints); behavior for authenticated callers is unchanged. The non-route `analyze._analyze_stream` generator retains an imperative resolve (it is not a route and has no rate-limiter).

---

## Deferred from the 2026-06-03 public-readiness fix plan

- **TEST-01 — Pytest non-fatal warnings (2026-06-03).** Pytest emits 123 non-fatal warnings (unawaited-coroutine in async mocks, `@pytest.mark.asyncio` on sync tests, `ORJSONResponse` deprecation). Cosmetic test-hygiene; full suite passes. Deferred.
- **UI-01 (contrast portion) — Dark-mode destructive text contrast (2026-06-03).** Dark-mode contrast of destructive error text (`text-destructive`) is below WCAG AA per Lighthouse. Proper fix is a theme-token adjustment affecting all destructive text; deferred to a theming pass.

## From the 2026-06-04 security remediation

- **SCA-GATE-01 — make the SCA Security workflow a required merge check (operator-only).** The `Security` workflow (pip-audit / npm-audit / osv-scanner, terminal `security-gate` job) runs on every PR and is green on master, but it is NOT yet a required status check. `ci.yml`'s required `gate` job uses `needs:` which can only reference jobs in the *same* workflow, so `security-gate` (a separate workflow) cannot be wired there. **Operator action:** add `Security / Security gate` as a required status check in branch protection (Settings → Branches → master), alongside the existing `CI gate (required status check)`.
- **Multi-admin legacy-data attribution (single-admin deployments unaffected).** Migration 0094 re-owns NULL-`user_id` rows on `paper_extractions`/`paper_entities`/zotero `paper_notes` to the single admin ONLY on single-tenant boxes (mirrors 0092). On a multi-admin box with pre-existing data: (a) historical Zotero notes keep their original `discovered_by` attribution (only forward syncs are attributed to the syncing user); (b) the entity batch-backfill (`knowledge_graph.py`) attributes to `papers.discovered_by`, which for system papers (`discovered_by IS NULL`) writes read-invisible NULL-user entity rows — the user-triggered single-paper extract correctly stamps the requesting user. Acceptable for the single-operator deployment norm; revisit if a true multi-admin instance with legacy data is targeted.
