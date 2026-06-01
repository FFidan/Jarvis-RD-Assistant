# Known Residual Risks

_Last updated: 2026-06-01_

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

**Current state:** not started. The build-vs-adopt decision — adopt an existing open agent framework vs. build natively on LiteLLM tool-calling plus the existing prompt harness — requires explicit sign-off before the spike is worth running. It is sequenced behind the cross-service auth work, the first performance phase, and an evaluation harness; see the [ROADMAP](../ROADMAP.md).

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

### H5 — Migration 043 live-fixture test deferred

Migration 043 uses a defensive PL/pgSQL constraint-name lookup. The live-fixture migration test is deferred to a future hardening pass with proper test infra.

**Reopen criteria:** when a migration test harness with a real ephemeral Postgres instance is available.

### L1 — Frozenset whitelist for `extra_sets` not enforced

The `extra_sets` allowlist is not yet enforced — the current guard is `isinstance(s, str)`. Treat all callers as trusted. A future hardening pass should add an allowlist of known-safe set names.

**Reopen criteria:** if `extra_sets` accepts caller-controlled input from any untrusted surface.

### L4 — S3 backup encryption relies on bucket SSE only

Backups are encrypted at rest by S3 server-side encryption but are not client-side encrypted before upload. A client-side openssl pipeline is deferred to a future infra pass.

**Reopen criteria:** when compliance requirements mandate end-to-end encryption of backup files.

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

---

## SMTP-EMPTY-STRING-1 — empty-string SMTP env vars silently accepted

**Symptom:** Setting any of `SMTP_HOST`, `SMTP_FROM`, `SMTP_USER`, or `SMTP_PASS` to an empty string is silently accepted by `SecretsSettings` (no Pydantic validator rejects `""`). `_EffectiveSmtp.deliverable` evaluates `bool("") == False`, so the magic-link sender falls through to the dev-mode logging path. **The operator sees nothing at startup; users do not receive magic-link emails; the failure is silent.**

**Impact:** HIGH (operator-facing silent failure, not data loss). Affects any deployment where SMTP env vars are mis-set to `""` rather than left unset — `""` and unset have different semantics, and only unset is correctly handled.

**Mitigation:** Add a `@field_validator` to `SecretsSettings.smtp_host/from/user/pass` rejecting empty-string values, OR add a startup-time health check that asserts the SMTP configuration is internally consistent. Four `xfail(strict=False)` tests in `libs/jarvis_common/tests/test_secrets_settings.py` document the gap and will auto-green when validators are added.

---

## BUILDER-STAGE-BUILD-UNHASHED-1 — builder stage installs `build` without `--require-hashes`

**Symptom:** All three service Dockerfiles (`paper_ingestion`, `learning_engine`, `telegram_bot`) use a Stage 1 `jarvis-common-builder` that runs `pip install --no-cache-dir build==1.2.2.post1` to produce the `jarvis_common` wheel. The `build` package and its transitives (`pyproject_hooks`, `packaging`) are installed without `--require-hashes` in the builder stage.

**Bounded by:** Stage 1 is ephemeral — only the produced wheel is lifted into Stage 2 (`COPY --from=jarvis-common-builder`). No builder-stage packages exist in the runtime image. Stage 2's `pip install --require-hashes -r constraints.txt` covers every runtime dep; the wheel itself enters via `pip install --no-deps`, so its declared transitives never trigger a fresh unverified resolution.

**Residual surface:** the wheel-build toolchain, not the runtime dependency surface.

**Operator attestation pattern (if hardening to zero unhashed pip invocations is desired):**

1. Generate a hashed constraint set for the builder toolchain:
   ```
   echo "build==1.2.2.post1" > /tmp/builder-req.in
   uv pip compile --generate-hashes /tmp/builder-req.in -o services/_shared/constraints-builder.txt
   ```
2. Add `COPY services/_shared/constraints-builder.txt ./` and change the builder-stage `RUN` to `pip install --require-hashes -r constraints-builder.txt`.

Deferred per YAGNI — Stage 1's build environment is not user-reachable and the wheel produced is gated by the runtime hash gate's `--no-deps` install pattern.

---

## Container Hardening Exceptions

These document intentional deviations from the container-hardening sweep, each with an accepted-risk rationale.

- **C-09 — `ollama/ollama` runs as root** (`docker-compose.yml` → `ollama` service). The upstream image requires uid 0 for GPU device-node access: `/dev/nvidia*` device nodes are owned by root and require either a privileged container or root to open. Switching to a non-root user breaks the NVIDIA device mount. No non-root upstream variant exists (confirmed 2026-05-26). `security_opt: ["no-new-privileges:true"]` is already set as a partial mitigation. Reopen when the upstream image ships a non-root GPU-capable variant.
- **C-05 — vLLM user `1000:1000` write access to the HF cache** (`docker-compose.vllm.yml`). The vLLM service runs as `user: "1000:1000"` with `HF_HOME` on a named volume. If that volume was previously populated as root, the first non-root startup may fail with a permissions error; remove the volume before the first non-root run so Docker re-creates it owned by uid 1000. One-time operator action; document in the setup runbook if vLLM is promoted to production.
- **SC-09 — `requirements-optional.txt` floor-pins are informational only.** The hashed security boundary lives in `constraints-optional.txt`, pinned with sha256 hashes verified at install (`pip install --require-hashes`). `requirements-optional.txt` is auto-generated from `pyproject.toml` and may not be hand-edited (the `check-python-deps` pre-commit hook enforces parity).
- **C-06 — vector `docker.sock` access; `cap_drop: [ALL]` is defense-in-depth only.** The vector log shipper mounts `/var/run/docker.sock:ro`; `cap_drop: [ALL]` removes Linux capabilities but socket access is governed by uid/gid, so vector can still `docker inspect` other containers. The proper fix is structural (swap `docker.sock` for a syslog/fluent-bit forwarder, or run vector outside the docker network) — deferred as it would touch the logging architecture. Reopen when a log-routing redesign is in scope.
