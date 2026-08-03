# Risk Register

_Last updated: 2026-08-03_

_Known residual risks and accepted operational/code-quality deferrals._

This document tracks acknowledged-but-deferred risks in JARVIS RD Assistant. Each entry states the rationale for deferring the full fix and the criteria that would reopen it. Resolved or rejected findings, plus internal CI and test-infrastructure tracking, are archived separately and are not part of the published site.

Related docs:

- [ARCHITECTURE.md](ARCHITECTURE.md) — runtime boundaries affected by residual risks.
- [SECURITY.md](SECURITY.md) — threat model and hardening checklist.

---

## v1.2.0 accepted boundaries

### Sanitized KaTeX still needs URL and image guards

**Why accepted (2026-07-20):** Markdown is sanitized before KaTeX rendering, so the HTML produced by KaTeX is not passed through the sanitizer a second time. Separate URL and image guards reduce the exposed surface.

**Reopen criteria:** a sanitizer or KaTeX change bypasses either guard, or a rendered document demonstrates an unsafe URL or image load.

### DNS can change after SSRF validation

**Why accepted (2026-07-20):** PDF URLs are validated before fetching, but DNS rebinding can change a hostname's destination between validation and the later HTTP request.

**Reopen criteria:** a supported deployment needs stronger protection against that time-of-check/time-of-use window.

### API-key login gate cache is process-local

**Why accepted (2026-07-20):** the supported deployment runs a single application worker, so its gate cache is local to that process.

**Reopen criteria:** multi-worker application deployment becomes supported or the gate needs shared invalidation.

### Telegram pairing tokens are stored in plaintext

**Why accepted (2026-07-20):** pairing tokens are 128-bit, single-use, and expire after 15 minutes; plaintext storage keeps the one-time pairing check simple.

**Reopen criteria:** token exposure, a longer-lived pairing flow, or a threat model that requires hashed-at-rest pairing secrets.

### Setup-status endpoint exposes a deployment fingerprint

**Why accepted (2026-07-20):** unauthenticated `/api/setup/status` reports first-run state, setup mode, hardware tier and vendor, access mode, model-backend state, and SMTP presence and reachability. The sign-in and setup screens need these fields before authentication, but together they reveal a deployment fingerprint.

**Reopen criteria:** the response gains fields beyond that minimal wizard-required state.

### Expired sessions have a 24-hour offline-review grace period

**Why accepted (2026-07-20):** the grace period is intentional so an interrupted offline review session can be completed.

**Reopen criteria:** the product no longer supports offline review, or the grace period becomes incompatible with the session threat model.

### Backup-manifest HMAC trust boundary

**Why accepted (2026-07-20):** after upgrade, signed manifest sidecars authenticate a complete archive set. This does not authenticate pre-upgrade legacy local sets, and cannot protect against an attacker who can replace both the archives and the backup secrets or signing key.

**Reopen criteria:** the backup format, key custody model, or legacy-restore policy changes.

---

## v1.1.3 access-mode and setup entries

### AMD/Intel Vulkan acceleration — Experimental, not Supported

**Finding:** the opt-in `--gpu vulkan` path passes `/dev/dri` and `OLLAMA_VULKAN=1` into the Ollama container, joining the host's `video`/`render` groups by numeric GID (`JARVIS_VIDEO_GID`/`JARVIS_RENDER_GID`, resolved from the host at install time — a group *name* would resolve against the container image's own `/etc/group`, not the host's).

**Current state:** the device passthrough and env wiring are real and exercised in `docker-compose.vulkan.yml`, but validation confidence is lower than the CUDA/ROCm paths — reach and actual acceleration depend on the host's Vulkan ICD, which JARVIS does not inventory. Classified Experimental, never Supported.

**Reopen criteria:** when enough field reports (`docs/manual/hardware-support-matrix.md#reporting-your-hardware`) accumulate to promote the tier, or an upstream regression is found.

### Raw-IP LAN access is health-check only

**Finding:** the `raw-ip-lan` route (`route_claims` in `scripts/setup_lib.sh`)
has no setup-token, cookie, or passkey contract. Nginx serves only the static
`/health/jarvis` marker to remote LAN clients and returns HTTP 403 for every
other path.

**Why accepted:** this gives operators a narrow reachability check without
turning plain LAN HTTP into an application origin. Family access uses a named
private HTTPS route configured by setup.

**Reopen criteria:** only if the raw-LAN route is proposed for application use;
that would require a new transport and authentication design.

### Pre-1.1.3 setup links used a query string, not a fragment

**Finding:** before this release, `print_setup_link` (`scripts/setup_lib.sh`) built the click-to-finish link as `/setup?setup_token=…` — a query string rides the request line and can land in access logs and `Referer` headers. It now builds `/setup#setup_token=…`, a URL fragment the browser never sends to the server.

**Current state:** the fix is forward-only. Links already printed to a pre-1.1.3 terminal's scrollback keep the old query-string form and still work — the onboarding wizard (`frontend/src/pages/OnboardingWizard.tsx`) accepts both forms for backward compatibility — but a query-string link that was ever pasted somewhere logged (chat history, a ticket) should be treated as exposed; regenerate the token via `scripts/init-secrets.sh` if that's a concern on an instance that hasn't finished first-admin bootstrap yet.

**Reopen criteria:** none — this is a completed, one-way fix; documented here for operators auditing old scrollback/logs.

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
magic-link email. A signed-in administrator can still create a 24-hour invite
link or a 15-minute sign-in link and share it through a private channel.
Multi-user access therefore remains usable, but account recovery depends on an
active administrator or the owner recovery key until SMTP is configured.

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

## Frontend supply-chain advisories

### react-router RSC-mode CSRF (GHSA-qwww-vcr4-c8h2) — not reachable

**Finding:** `osv-scanner` / `npm audit` flag `react-router` 7.18.2 (pulled by `react-router-dom`) under GHSA-qwww-vcr4-c8h2 — a CSRF bypass that can execute a router action before a 400 response is returned, in React Router's RSC (React Server Components) mode.

**Why accepted:** the frontend is a client-only SPA that uses `react-router-dom` declarative routing exclusively. The RSC packages, server-action pipeline, and the APIs the advisory concerns are never imported or configured, so the vulnerable code path is unreachable in this deployment. No compatible fix is installable either: the advisory is patched in react-router core 8.3.0, but `react-router-dom` — the app's actual routing dependency — has published no v8 release, so it cannot pull the patched core, and downgrading below 7.12.0 drops required features. The suppression is scoped to this one advisory id in `frontend/osv-scanner.toml`.

**Reopen / removal trigger:** remove the suppression and this entry when the frontend adopts react-router >= 8.3.0 (or a back-ported patched v7 release appears), or immediately if the app ever introduces React Router RSC mode or server actions. Suppression review date: 2026-10-15.

_The companion brace-expansion advisory (GHSA-mh99-v99m-4gvg), previously suppressed here, was resolved in v1.2.3 by pinning the transitive dev-only v1 line to 1.1.17; its suppression is removed._

---

## Further known residual risks

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

### Unused router-factory parameter (low priority)

One consolidation item remains as low-priority code-quality debt:

- `build_jobs_router(service_name=…)` accepts a `service_name` parameter that no code path currently uses. Retained to avoid touching every call-site; remove when the router is next refactored.

**Reopen criteria:** the router factory or its call sites are refactored.

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

### Restore destructive-sentinel: SIGKILL mitigation and operator-clear requirement

`scripts/restore.sh` writes a durable `.destructive` sentinel before the first
database drop. While this sentinel is present, the app returns HTTP 503 on all
non-exempt routes regardless of sentinel age. A `SIGKILL` mid-restore therefore
cannot leave the stack serving a partly restored database.

**Operator action required to resume:** a clean same-host, inbox, or older-version
restore clears both maintenance markers automatically. A failure after the
first drop keeps `.destructive` in place. For a same-host restore, recover from
the safety restore point. For an inbox restore on a fresh host, correct the
reported cause and retry the signed off-site set. Do not clear the marker until
the databases and services have been checked; a successful retry clears it
automatically. See the [backup and restore guide](manual/backup-and-restore.md).

---

## Deferred low-value / high-churn cleanups

The following items were deliberately deferred as low-value or high-churn. Each is behavior-neutral debt, not a correctness or security risk.

- **`_owner_headers` promotion.** The Telegram `_owner_headers` helper stays at its current call-site location (pinned by tests); promoting it to a shared module is not worth the churn.
- **Service-layer `HTTPException` coupling.** A few service functions raise FastAPI `HTTPException` directly rather than a domain error the router translates. Left as-is; decoupling is a broad refactor.
- **`db_helpers` split.** `paper_ingestion`'s `db_helpers` mixes a few concerns; splitting it touches many importers for marginal gain.
- **`JOB_HANDLER_OWNER` / `noop.test` queue.** The job-handler-owner `Literal` typing and the `noop.test` queue entry are retained as-is; tightening them is cosmetic.
- **Redundant My-Day journal fetch.** `GET /api/my-day/journal` overlaps the nullable `journal` field already returned by the my-day-bundle; the separate fetch is kept to avoid a frontend refactor.
- **`jarvis_common` test-code wheel exclusion.** The `jarvis_common` wheel ships its `testing_*.py` modules (~25 KB). A `packages.find` exclude is ineffective for top-level modules (only a `testing_sidecars/` subpackage would drop); a correct fix means relocating six modules into a `jarvis_common/testing/` subpackage plus updating 100+ test imports — not worth the gain. Runtime-safe regardless (zero non-test runtime imports of `jarvis_common.testing*`).

**Intended behavior note — auth-first ordering (auth-idiom migration).** `paper_ingestion` route handlers now resolve identity via `Depends(current_user_id_strict)` (previously an imperative in-body call). Consequence: an *unauthenticated* request to a migrated endpoint now fails authentication (401) *before* request-body validation runs, whereas the old imperative order could surface a 422/400 body-validation error first. This is intended and more consistent/secure (uniform auth-first across all endpoints); behavior for authenticated callers is unchanged. The non-route `analyze._analyze_stream` generator retains an imperative resolve (it is not a route and has no rate-limiter).

---

## Deferred theming / test-hygiene items

- **Dark-mode destructive text contrast.** Dark-mode contrast of destructive error text (`text-destructive`) is below WCAG AA per Lighthouse. Proper fix is a theme-token adjustment affecting all destructive text; deferred to a theming pass.

## Ambiguous legacy derived-output ownership

Migration 0094 can assign historical NULL-owned notes and structured
extractions only when an upgraded instance has exactly one administrator. On a
pre-baseline installation with multiple administrators, ambiguous legacy rows
remain unassigned and therefore inaccessible instead of being guessed into one
user's workspace. New writes always carry the initiating user's identity, and
paper visibility no longer depends on `discovered_by`.

**Reopen criteria:** an operator needs to recover ambiguous pre-baseline output
from a multi-administrator installation; attribution then requires a deliberate
data-specific migration rather than a global default.
