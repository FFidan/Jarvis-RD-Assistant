# Changelog

All notable changes to JARVIS RD Assistant are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

## Unreleased

### Added
- **SMTP Reply-To and sender display name.** Two optional SMTP fields are now available in both the first-run wizard and Settings → System → Email / SMTP: a **Reply-To address** (routes user replies away from the From address) and a **Sender display name** (sets the friendly name in the From header, e.g. `JARVIS RD <login@your-domain.dev>`). Both can be set via the UI or the new `SMTP_REPLY_TO` / `SMTP_FROM_NAME` environment variables; leaving a field blank clears the stored value.
- **SMTP misconfiguration warning and in-place test send.** Settings → System → Email / SMTP now shows an amber warning banner when the effective mail relay is not deliverable (partial configuration, empty-string env vars, or no relay set). The **Save & send test email** button — previously only in the wizard — is now also available in Settings, with an optional test recipient and inline error reporting.

## v0.8.5 (2026-06-14) — Trustworthy, Comprehensible & Credible

A polish release focused on trust, plain language, and a clean, public-ready
codebase. Researcher-facing screens no longer surface implementation jargon,
real settings dead-ends and false-failure states are fixed, and the project's
toolchain is brought up to date.

### Fixed
- Zotero "Test connection" reports success correctly (was a false "Failed" on a valid key).
- Access-mode status reflects the saved value and shows the exact restart command; a "restart pending" indicator persists across reloads.
- The SMTP test and magic-link delivery work on plain port-25 relays, not only STARTTLS/implicit-TLS ports.
- Pulse scoring uses a capable model and records an honest "ranked heuristically" reason when LLM scoring is unavailable.
- Project updates return real paper and open-question counts; empty project, task, and milestone names are rejected.
- Telegram review distinguishes a load error from "all caught up" instead of falsely reporting completion.
- No settings dead-ends: every hardware tier has a selectable model, and the AI-backend page guides you instead of showing a bare "no candidates".

### Clarified (plain language)
- System Health reads in plain language — labels, a verdict word, per-service consequences, and an overall summary.
- Logs filters distinguish Severity from Area, with visually distinct chips.
- A single authoritative model-settings page (advanced backend/hardware controls move behind a disclosure); plain model names, fit badges, and selector copy.
- Pulse optional signals are locked when unavailable, with each prerequisite named.
- Onboarding, login, My Day, and paper surfaces use researcher language (no internal terms like context-window internals, "RAG", routing tiers, raw cron, or env-var names on screen).

### Hardened
- The Telegram bot token is stored as a secret; the self-hoster setup scripts generate and validate every required secret through one generator.
- Infra-event uploads are bounded by streamed size; paper-summary reads are scoped to the owner.

### Docs & internal
- Replaced developer-rig GPU names with hardware-tier descriptors; removed internal tracking identifiers from comments and tests; neutralized key-rotation guard wording.
- Re-baselined the database schema into a single clean baseline; localized de-duplication across source plugins, the request layer, and lifecycle responses.
- Upgraded the frontend toolchain (ESLint 10, Tailwind CSS 4) and wired the end-to-end test suite into CI.

## v0.8.0 (2026-06-13) — Trustworthy & Frictionless

A reliability- and trust-focused release. The goal: a researcher with no CS
background can install JARVIS, get a correct full-coverage summary and a
trustworthy Ask answer — with the right model for their hardware actually
running — without editing a file or learning the word "num_ctx".

### Highlights

- **Install that survives a bare machine.** `./setup.sh` no longer crashes on
  hosts without PyYAML, checks Docker daemon access up front, keeps your data
  when you re-run it, and streams the first model pull so the long download is
  visible. Honest CPU/GPU speed expectations are documented up front (including
  macOS).
- **Model choices are real, or honestly pending.** Changing the main/quick model
  in Settings now actually re-routes the LLM and survives a restart. If the model
  service is briefly unavailable your choice is saved and applied automatically
  within about 30 seconds, with a clear "applying" badge — never a silent revert
  to the old model.
- **The right model out of the box.** On first run JARVIS picks the largest model
  your GPU can comfortably run (keeping the embedder resident) plus a safe
  reading window to match — no manual tuning — and tells you what it picked.
- **Long papers are read in full.** Summaries and flashcards now read 100% of a
  paper via a map-reduce pass instead of only the opening pages, with a quiet
  note showing how many passes it took. Verified quotes are only ever taken from
  text the model actually read.
- **One reading window.** The context size is a single plain-language slider
  ("Reading window") in Settings → Models that flows through the whole pipeline;
  raising it speeds up GPU analysis and is bounded to a memory-safe maximum.
- **A trust layer you can read.** Ask answers carry an honest confidence badge
  with in-app definitions, short factual answers no longer show a misleading
  "unverified" banner, and the weekly digest's theme verification is real.
- **Frictionless research flow.** You can Ask as soon as a paper is analysed
  (no Topics required), there is one "Analyze" verb everywhere, the feed
  surfaces are named consistently (Library / Discover), errors tell you what to
  do next, and the Pulse deck can be regenerated where it lives.
- **A gentler learning curve.** A new simple navigation mode shows just the daily
  essentials for first-time users (everything else one click away), and the model
  settings now read in plain language.
- **Hardening.** Card generation degrades gracefully on provider errors,
  cross-tenant vector isolation is enforced, contradiction scans de-duplicate
  concurrent runs, and several configuration values are now validated. The
  frontend build moved to Vite 8's Rust toolchain (Rolldown), which also drops
  a vulnerable build-time dependency.

### Upgrade Notes

- **Re-run `scripts/init-secrets.sh` before `docker compose up`.** This release
  adds a required `litellm_salt_key` secret; compose will not start without it.
- **Model configuration now lives in the LiteLLM admin database**, delivered via
  the model-management API rather than the YAML file. Fresh installs need no
  action; existing installs reconcile automatically on first boot. The switchable
  `smart`/`fast` aliases are no longer seeded from `litellm/config.yaml`.
- **The reading window (`num_ctx`) is now a single value** on the Settings →
  Models slider. The `LLM_SMART_NUM_CTX` environment variable remains the
  boot-time default/fallback only — you no longer keep it in sync by hand.

## v0.7.0 (2026-06-11) — Research Quality

Focused research-quality release: self-contained flashcards, more reliable AI
summaries, a smarter Ask pipeline, server-side library search, and several
mobile refinements.

### Upgrade Notes

- **LiteLLM `num_ctx` migration.** Summaries and flashcards now budget their
  prompt input to the model's context window instead of sending a fixed amount
  that silently overflowed it. If you have customised `num_ctx` in
  `litellm/config.yaml`, set the new `LLM_SMART_NUM_CTX` environment variable
  to the same value and recreate the proxy and its consumers
  (`docker compose up -d --force-recreate litellm paper_ingestion
  learning_engine`). When the variable is unset the app assumes the stock
  8192-token context.
- **New optional Ask tuning knobs.**
  `RAG_RELATIVE_SCORE_CUTOFF` (default `0.85`) gates retrieved sources by their
  relevance relative to the top-scoring result for each query facet.
  `RAG_MIN_RERANK_SCORE` sets a hard floor when the optional reranker is
  enabled. Both can be left unset to accept the defaults.

### Added

- **Library search.** The filter box in the Library and Inbox now performs a
  server-side full-text search (title, author, and abstract with stemming) when
  three or more characters are typed. Previously the box only filtered the
  already-loaded page.
- **Regenerate Summary action.** A "Regenerate Summary" button is now available
  in the paper sidebar so you can re-run the summarisation step without
  re-analysing the whole paper.
- **Calibrated confidence badge.** The Ask answer badge now reflects the
  degree of grounded support (Verified / Mostly verified / Partially verified /
  Unverified) rather than
  a simple pass/fail. A warning is shown only for low-confidence and unverified
  answers.

### Changed

- **Flashcard fronts are self-contained.** A generic-question filter prevents
  cards whose front question only makes sense with the paper in hand (e.g. "What
  is the main contribution of this paper?"). Generation prompts are rewritten to
  produce standalone questions. If no suitable cards can be generated, a clear
  message with guidance is shown rather than a synthetic fallback card.
- **Summary reliability on small models.** The output budget for AI summaries
  is raised, and the prompt input is budgeted to the model's context window via
  the new `LLM_SMART_NUM_CTX` setting (paired with LiteLLM's `num_ctx`). When
  the LLM returns a summary that does not pass quality verification, the result
  is still displayed with a "low-confidence" label instead of being silently
  replaced by an error string.
- **Cross-references are semantic-only.** Cross-reference suggestions between
  papers are now based on embedding similarity rather than keyword overlap,
  eliminating spurious links caused by shared common words.
- **Ask context carries across follow-ups.** Follow-up questions in the Ask
  workspace now include the current conversation context, enabling coherent
  multi-turn research dialogues.
- **Ask source panel wording.** Retrieved passages are now labelled "Source
  Passages" in the UI. Numeric relevance scores, model names, and internal job
  IDs have been removed from the answer view.
- **Relevance gate for retrieved sources.** Retrieved passages are filtered by
  a relative-score gate per query facet before being used to construct an answer.
  An optional reranker floor (`RAG_MIN_RERANK_SCORE`) is applied when the
  reranker is enabled.
- **Feed cards stack on mobile.** Research feed cards now stack vertically on
  narrow phone viewports. Topic rows wrap correctly, the Discover heading is
  visible, and the install-banner dismissal is persisted across page loads.

### Security / Dependencies

- The PyTorch advisory CVE-2025-3000 is triaged in the security scan
  configuration. No patched wheel is available from upstream at this time; the
  vulnerability affects PyTorch model serialisation and is not reachable via
  JARVIS's default Ollama-based inference path.

---

## v0.6.0 (2026-06-06) — first public release

First public release of JARVIS RD Assistant. Highlights since v0.5.0: **per-user
multi-tenant isolation**, **GDPR-purge correctness**, **SMTP-SSRF + credential
encryption**, **token-only Telegram pairing** and the Telegram→REST decoupling,
**GPU/setup foolproofing**, **whole-app mobile**, and a **unified onboarding wizard**.

### Upgrade Notes

- **Cloud LLM API keys must be re-entered.** API keys for cloud LLM providers
  (e.g. OpenAI, Anthropic) that were set via the Settings UI before this release
  must be re-entered by an admin — they are now stored deployment-wide
  (system-scoped) rather than per-user.

### Security

- **Per-user tenant isolation** (migration 0094): all source API keys, LLM
  provider keys, and Zotero credentials are now strictly scoped per user.
  Extractions, entity records, and Zotero sync are stamped and filtered by
  `user_id`. Cross-user visibility of these records is no longer possible.
- **Cloud LLM provider keys are now system-scoped and admin-gated.** Keys for
  cloud providers (OpenAI, Anthropic, etc.) are stored at the deployment level
  and may only be configured by an admin.
- **SMTP-SSRF hardening.** The SMTP host is validated against a blocklist of
  non-public address ranges at both save and send time. An
  `ALLOW_PRIVATE_SMTP_HOST` escape-hatch env var is available for on-premises
  mail servers.
- **Credential/auth hardening.** Telegram `bot_token` is now Fernet-encrypted
  at rest. Advisory locks guard concurrent authentication flows. Admin and setup
  log entries hash email addresses before writing to the log.
- **Source API keys (S2/OpenAlex/PubMed) encrypted at rest.** All per-user
  source credentials are stored encrypted via the existing MultiFernet scheme.
- **PubMed/OpenAlex author-parameter injection hardened.** Author search
  parameters are validated and sanitised before reaching the upstream API.
- **PDF-download SSRF filter blocks CGNAT.** The URL pre-flight check now
  rejects CGNAT and other non-routable ranges in addition to RFC-1918 space.
- **Rate-limiter ignores malformed X-Forwarded-For hops.** Invalid IP tokens in
  the XFF header are silently skipped instead of causing a 500.
- **`/infra-events` rejects oversize request bodies.** A hard body-size limit is
  enforced on the infrastructure-event endpoint.
- **Telegram base-URL scheme validation.** The configured Telegram API base URL
  must use an `http(s)://` scheme; other schemes (e.g. `javascript:`) are
  rejected at config load to prevent XSS / open-redirect via digest links.

### Changed

- **Unified onboarding wizard.** The former two-wizard flow (pre-auth `/first-run` + post-login `/setup`) has been replaced by a single **Onboarding Wizard** gated by the pre-auth `/api/setup/status` endpoint. The wizard spans the auth boundary internally: it walks system check → SMTP → admin account creation & sign-in → cloud LLM keys → first topic → automation schedule → source API keys → Telegram pairing → done. Old `/first-run` and `/setup` deep links redirect to `/`. The admin-create step is skipped when an admin already exists (resuming setup).

- **`/api/setup/status` now returns `setup_completed`.** The pre-auth setup status endpoint (always HTTP 200, no session required) now includes a `setup_completed: bool` field alongside the existing `configured` and hardware fields. The onboarding gate keys on this field.

- **Telegram bot pairing is token-only.** The bot authenticates chats via the `/pair <token>` flow (token from Settings → Integrations → Telegram). The legacy `/start PAIR_<code>` dashboard-code pairing path is removed, and the `TELEGRAM_CHAT_ID` env var is superseded by `/pair` for identity — it no longer authorises a chat on its own and is retained only as an optional override for the outbound message target. The bot no longer writes to the database directly — all product data flows through the service REST API.

### Fixed

- **GDPR purge succeeds in multi-user deployments** (migration 0095):
  `paper_entities` and `pulse_models` now cascade on user delete, so a
  user-deletion request can no longer fail permanently due to a foreign-key
  constraint.
- **Topic facet filters the research feed.** Clicking a topic facet in the
  Research view now correctly narrows the paper feed; previously it highlighted
  but did not filter.
- **GDPR export includes per-user extractions and entities.** The data-export
  package now contains the logged-in user's paper extractions and entity rows.
- **`daily_log` analytics no longer NULL-breaks.** Aggregate queries over the
  daily log table are guarded against NULL entries that previously caused 500
  errors in analytics.
- **Batch-save skips re-analysis of already-processed papers.** Saving a paper
  that already has a completed analysis no longer enqueues a redundant
  `paper.analyze` job.
- **Local-PDF scan attributes papers to the scanning user.** Papers discovered
  via local-PDF scan are now owned by the user who initiated the scan rather
  than being left NULL-owned.
- **Cross-paper RAG retrieves the caller's full library.** The retrieval step in
  multi-paper Q&A now correctly searches the requesting user's entire saved
  library rather than a subset.
- Assorted frontend validation hardening and dead-code cleanup.
- Source-layer `Retry-After` handling and fetch-recording de-duplicated across
  arXiv, Semantic Scholar, OpenAlex, and PubMed source adapters.
- **Models-ready false negative.** The onboarding wizard's system check no longer requires a hardcoded `qwen3:14b` model. "Models ready" now means: the embedder is present (any model matching the configured embedding model prefix, e.g. `qwen3-embedding:*`) AND at least one `qwen3:` chat model is present (`qwen3:4b`, `qwen3:8b`, or `qwen3:14b`). The default install (`setup.sh` → `qwen3:8b` + `qwen3-embedding:4b`) correctly reports ready. The check also distinguishes "still pulling" from "Ollama unreachable".
- **Pomodoro auto-start from stale persisted timer.** A Pomodoro session that was still running when the browser closed no longer auto-starts on the next page load. The timer state is correctly treated as stale across sessions.
- **Cross-user Pomodoro / dismissed-flag state leak.** Timer and dismissed-flag state no longer leaks between users on a shared browser.
- **My Day — calm empty state for the Pulse hero card.** When no Pulse deck exists yet, the My Day Pulse hero card shows a calm "No Pulse for today yet — generate one" call-to-action instead of a red error panel. Red error UI is reserved for genuine backend failures.
- **Mobile responsive fixes.** Projects rail, admin tables, analytics KPI band, mobile facet drawer, TopBar, My Day layout, and the chat surface are now correctly laid out on narrow viewports.
- **Logs preset filters restored on load.** The Logs page preset now re-applies its filter selections when the page is loaded or navigated to.
- **Single-tenant background-job attribution.** Task-completion and paper-summary jobs attribute activity to the owner's account (previously recorded as NULL in single-tenant deployments).

### Migrations

- **0092** — Re-owns legacy NULL-owned product rows (projects, tasks, milestones, daily_log) to the single admin account (single-admin deployments only).
- **0093** — Adds `papers.zotero_citation_key` for Zotero citation-key push.
- **0094** — Per-user scoping of extractions, entity records, Zotero sync, and notes.
- **0095** — Cascades `paper_entities` + `pulse_models` on user delete (GDPR purge correctness).

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
- Published an MkDocs-Material operator and developer documentation site to GitHub Pages, including a complete end-user guide covering every surface plus plain-English sign-in and account-recovery steps.
- Added a public `ROADMAP.md` and corrected a batch of documentation drift (migration counts, deprecated environment variables, and stale internal links).
- Documented the `setup.sh --check` pre-flight, single/multi-user modes, and the source HTTP-cache environment variables.



## Pre-public development (v0.1 – v0.4.1)

The v0.1 through v0.4.1 releases represent the full private development phase. The core RAG pipeline was built across this period: multi-source paper discovery (arXiv, Semantic Scholar, OpenAlex, PubMed), PDF extraction with page-level citation provenance, a three-stage LLM-reranked Pulse recommendation engine, and a semantic knowledge graph with entity extraction and contradiction detection. Spaced-repetition learning cards (FSRS) and a daily executive-function interface (My Day, Pomodoro timer, journal, project tracking) were added alongside the recommendation system. Multi-tenancy and security hardening — magic-link authentication, strict user_id scoping across all data paths, per-user FSRS and recommendation state, cross-user isolation CI gates, Docker Secrets, and a container-hardening sweep — were progressively applied from v0.2 onward. The job infrastructure was migrated from a custom worker to procrastinate-backed async task queues with SSE progress streaming. Observability tooling (Langfuse, Sentry, structured audit logging) and a one-shot installer wizard were added in v0.3–v0.4. The v0.4.1 release closed the last known cross-tenant data leaks and completed a full adversarial-review pass before the v0.5.0 pre-release consolidation.
