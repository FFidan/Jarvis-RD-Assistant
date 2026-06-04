# Changelog

All notable changes to JARVIS RD Assistant are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Unified onboarding wizard.** The former two-wizard flow (pre-auth `/first-run` + post-login `/setup`) has been replaced by a single **Onboarding Wizard** gated by the pre-auth `/api/setup/status` endpoint. The wizard spans the auth boundary internally: it walks system check → SMTP → admin account creation & sign-in → cloud LLM keys → first topic → automation schedule → source API keys → Telegram pairing → done. Old `/first-run` and `/setup` deep links redirect to `/`. The admin-create step is skipped when an admin already exists (resuming setup).

- **`/api/setup/status` now returns `setup_completed`.** The pre-auth setup status endpoint (always HTTP 200, no session required) now includes a `setup_completed: bool` field alongside the existing `configured` and hardware fields. The onboarding gate keys on this field.

### Fixed

- **Models-ready false negative (SYSCHECK-01).** The onboarding wizard's system check no longer requires a hardcoded `qwen3:14b` model. "Models ready" now means: the embedder is present (any model matching the configured embedding model prefix, e.g. `qwen3-embedding:*`) AND at least one `qwen3:` chat model is present (`qwen3:4b`, `qwen3:8b`, or `qwen3:14b`). The default install (`setup.sh` → `qwen3:8b` + `qwen3-embedding:4b`) correctly reports ready. The check also distinguishes "still pulling" from "Ollama unreachable".

- **Pomodoro auto-start from stale persisted timer (POMO-01).** A Pomodoro session that was still running when the browser closed no longer auto-starts on the next page load. The timer state is correctly treated as stale across sessions.

- **Cross-user Pomodoro / dismissed-flag state leak.** Timer and dismissed-flag state no longer leaks between users on a shared browser.

- **My Day — calm empty state for the Pulse hero card (RED-ERROR-EMPTY-STATE).** When no Pulse deck exists yet, the My Day Pulse hero card shows a calm "No Pulse for today yet — generate one" call-to-action instead of a red error panel. Red error UI is reserved for genuine backend failures.

- **Mobile responsive fixes.** Projects rail, admin tables, analytics KPI band, mobile facet drawer, TopBar, My Day layout, and the chat surface are now correctly laid out on narrow viewports.

- **Logs preset filters restored on load.** The Logs page preset now re-applies its filter selections when the page is loaded or navigated to.

---

## v0.5.0 (2026-06-02)

### Changed / Breaking

- **Telegram bot pairing is now token-only.** The bot authenticates chats
  exclusively via the `/pair <token>` flow (token generated in Settings →
  Integrations → Telegram). The legacy `TELEGRAM_CHAT_ID` environment variable
  and the dashboard-code pairing path are removed. Unpaired chats are prompted
  to `/pair`. Existing `TELEGRAM_CHAT_ID` values can be removed from `.env`.

### Fixed

- Task-completion and paper-summary background jobs now correctly attribute
  activity to the owner's account in single-tenant deployments (previously
  recorded as NULL).

### Migrations

- **0092** — Re-owns any legacy NULL-owned product rows (projects, tasks,
  milestones, daily_log, etc.) to the single admin account. Runs automatically
  on upgrade; only activates when exactly one admin user exists.

---

## v0.5.0 (2026-05-24)

### Overview

This release consolidates six weeks of internal audit and hardening in preparation for the first public launch. Approximately 120 findings across security, correctness, architecture, and developer experience were addressed. The core RAG pipeline, spaced-repetition learning system, and daily executive-function interface are now fully hardened for multi-tenant self-hosting. Major work included a cross-tenant audit (all data paths verified user-scoped), a dependency security pass (PDF engine migrated to Docling, closing transitive CVEs), and extensive public-readiness remediation. The migration history was squashed into a single `db/init.sql` baseline with new migrations starting at 0089.

### Detailed changes (2026-05-26)

A six-week internal audit-and-remediation pass closed roughly 120 findings ahead of the first public release. The themes below capture user-visible and operator-visible changes; commit-level detail follows in the per-area sections.

**Security.** The background-job Server-Sent Events stream now requires an authenticated session — it previously accepted unauthenticated subscriptions and returned job state for the NULL user. All cross-user data paths were re-audited: project recommendations, paper-source feedback, author alerts, and review-deck queries are now scoped to the logged-in user, and admins cannot read other users' research data. Prompt-injection vectors in PDF body text, paper titles, discovery snippets, and tracked-author bios are stripped before reaching the model with a documented prompt-shape contract enforced by an AST check. Container processes drop privileges, run with `no-new-privileges` set, and ship with a root-level `.dockerignore` so secret files and host-bound paths cannot accidentally land in the build context. Lock-file integrity (Python `uv.lock`, npm `package-lock.json`) is now verified against registry pins at install. Append-only audit logs reject `UPDATE`/`DELETE` at the PostgreSQL rule level, and the pairing-code length, rating regex, and ProjectManager method signatures were tightened against malformed input.

**Correctness.** Several long-standing cross-tenant bugs were fixed: the recommender's project query now filters by `user_id`; author-alert dedupe is per-user instead of global; the Zotero push flow no longer leaks `paper_id` across sessions. A handful of API surfaces that previously returned 200 with inconsistent envelopes now return one shape, and the streaming Chat error path surfaces transport errors to the UI instead of swallowing them. The `paper_sources` table and `PaperSource` abstract base were brought into symmetry so the catalog the UI shows matches what the ingestion job actually runs.

**Architecture.** Two oversized modules were split by responsibility: `entities.py` (814 LOC) became a typed router + a Postgres adapter + a Qdrant adapter; `routers/settings.py` was decomposed by settings domain. A new internal Telegram bot API removes the previous Telegram-bot → paper-ingestion DB-coupling. The migration history was squashed: the 88-file pre-v0.5.0 chain became a single `db/init.sql` bedrock with new migrations starting at `0089`, and `tests/test_baseline_invariants.py` pins the schema invariants.

**Developer experience.** Continuous integration now enforces type-check (Pyright zero errors), a test-shape contract (each test belongs to one of four documented shapes), the LLM prompt-shape AST check, and PII / burned-secret allowlists. The CI workflow was migrated to `astral-sh/setup-uv@v6` with a Python 3.12 pin and `uv sync --frozen`, cutting wall-clock from 8–15 minutes to 4–5 minutes. A pre-commit hook runs the same gates locally.

**Public-launch preparation.** This release ships a rewritten README with above-the-fold product screenshots, a Highlights section, and the four-audience deployment path; weekly `dependabot` updates for pip, npm, Docker base images, and GitHub Actions; structured GitHub issue templates (bug report, feature request) with security reports routed to a private GitHub Security Advisory; and a root `SECURITY.md` pointing to the threat model.

### Upgrade Notes

- **Migration baseline squashed.** The 88-file migration chain prior to v0.5.0 was consolidated into `db/init.sql` as the single baseline; new migrations start at 0089. The migration runner detects squashed-init state and applies forward without interruption — operators upgrading from v0.4.1 or earlier need no manual intervention. See `tests/test_baseline_invariants.py` for the schema invariants pinned.

### Security
- Cross-tenant project leak in recommender: `_refresh_recommendations_for_user` now scopes the projects query to `user_id`.
- Append-only `audit_log` (migration `0090_audit_log_append_only.sql`): blocks `DELETE`/`UPDATE` on the audit table via PG rule.
- Per-user author-alert dedupe (migration `0091_author_alert_log_user_dedupe.sql`): `ON CONFLICT (user_id, tracked_author_id, paper_id)` prevents cross-user alert suppression.
- Owner-override guard tests + audit-log emission.
- Pairing-code length bound, rate-card regex, mandatory `user_id` on 3 ProjectManager methods.

### Bug Fixes
- Email verification flag now respects SMTP exception path.
- Zotero BYTEA decode uses `crypto.resolve_secret_row` (memoryview-safe).
- Summarizer HTTPException propagation guarded at `paper_jobs.py:231, 270`.
- Unread guard in `feed_query.py` resolves contradictory WHERE composition.
- `vector_writer` role boot-time password drop guard.
- Three missing CI smoke secrets.
- `arxiv_source.py` parses `response.content` (bytes) instead of re-encoding `response.text`.
- `weekly_summary.py` ThemeOutput stays as Pydantic instance, no dict.get on LLM output.
- `pulse/job.py` degraded_reason OR-chain preserves earlier value (verified no change needed).
- 13 additional MEDIUM fixes (config validation, GDPR scoping, dynamic model field-name validation, CIDR cache, etc.).
- 9 FRONTEND error-sentinel + per-tab error handling fixes.
- 24 cross-cutting fixes: `decompose_query` doc catalog, sentry-init helper, fixture deduplication, `LockNotAvailableError` simplification, jobs throttle elapsed-seconds, `faux_qdrant` dim-mismatch + null-field guards, email format → replace, `_retry_after_seconds` cap at 3600, `_HAS_QWEN3` guard, `ScoredCandidate` frozen, scheduled `magic_link_tokens` purge, `SourceType.ZOTERO` enum, init-secrets.sh dedupe, profile.sh portable compose ps.

### Hardening
- Enhanced PostgreSQL connection robustness: `_spin_pg_container` adds post-`pg_isready` TCP socket probe (30s deadline + 250ms retries) to eliminate SSL-init race conditions on CI runners.

### Deferred / Documented
- Several architectural and infrastructure items documented in `docs/known-residual-risks.md` with reopen criteria for future releases.



### Documentation
- MkDocs-Material operator/developer docs site → GitHub Pages
- Refresh deferred backlog post CI-green program (Hermes, Performance&hardware-fit, 046/047-residual, installer/docs-site, Qdrant-re-embed-conditional)
- Correct stale mig-046 test comment
- Mark shipped --no-deps / discovery-reliability items DONE
- Fix 10 verified drift items (migration count, deprecated env, broken/stale refs, CHANGELOG regen) + archive superseded audits
- Add end-user guide (surfaces + plain-English sign-in/recovery), index in docs/README
- Canonical post-UI_v3 follow-ups execution plan
- De-link removed PomodoroTimer.tsx in 2026-05-02 decisions doc (UI_v3 deleted it; fixes check_agent_docs)
- Land 8 IA redesign specs + INDEX + parallelized execution plan
- Add companion docs site + complete user guide (Planned; UI-guide gated on redesign)
- Setup.sh --check + single/multi mode + source HTTP cache env vars
- Add public ROADMAP.md (shipped v0.4.1 / in-progress / planned Hermes+offline)
- Correct stale carried follow-ups (resolve_owner_chat_id NOT dead; py-spy/feed-500 closed in v0.4.1) + log v0.4.1-surfaced opens



## Pre-public development (v0.1 – v0.4.1)

The v0.1 through v0.4.1 releases represent the full private development phase. The core RAG pipeline was built across this period: multi-source paper discovery (arXiv, Semantic Scholar, OpenAlex, PubMed), PDF extraction with page-level citation provenance, a three-stage LLM-reranked Pulse recommendation engine, and a semantic knowledge graph with entity extraction and contradiction detection. Spaced-repetition learning cards (FSRS) and a daily executive-function interface (My Day, Pomodoro timer, journal, project tracking) were added alongside the recommendation system. Multi-tenancy and security hardening — magic-link authentication, strict user_id scoping across all data paths, per-user FSRS and recommendation state, cross-user isolation CI gates, Docker Secrets, and a container-hardening sweep — were progressively applied from v0.2 onward. The job infrastructure was migrated from a custom worker to procrastinate-backed async task queues with SSE progress streaming. Observability tooling (Langfuse, Sentry, structured audit logging) and a one-shot installer wizard were added in v0.3–v0.4. The v0.4.1 release closed the last known cross-tenant data leaks and completed a full adversarial-review pass before the v0.5.0 pre-release consolidation.
