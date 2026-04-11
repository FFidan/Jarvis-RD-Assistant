# Changelog

All notable changes to JARVIS RD Assistant will be documented in this file.

## [Unreleased]

## [1.2.0] - 2026-04-11

### Phase 1 Shipped — Discovery & Pulse subsystem

The overnight proactive paper discovery subsystem is complete. All six implementation layers landed, all acceptance criteria were met, and the eval harness passes (precision@10 = 100%, no-leakage = 0% on the synthetic labeled set). The full architectural design is embedded in `docs/PRD.md` §3.1.1 and §8.5 and in `AGENTS.md` under "Discovery & Pulse Subsystem".

- **Discovery & Pulse subsystem** — a proactive overnight paper discovery layer. An APScheduler job runs overnight, polls enabled external sources in parallel, scores candidates with a hybrid embedding + LLM pipeline, and persists a small curated card deck for morning delivery via the My Day widget and optional Telegram.
- **New source plugins** — OpenAlex (new plugin, free key required, cross-domain coverage) and PubMed (new plugin, enabled by default, optional key for rate limit upgrade). Both implement the extended `PaperSource` ABC with two new optional methods (`fetch_new_since`, `get_recommendations`). Existing arXiv and Semantic Scholar plugins are extended to implement the same methods.
- **PDF resolution chain** — `pulse/resolver.py` tries arXiv direct → Unpaywall fallback → cached failure marker. Called lazily when a Pulse card is saved. Also usable as a fallback in the existing ingestion pipeline when S2's PDF URL is broken.
- **Scoring pipeline (3 stages in Phase 1)** — Stage 1 embedding similarity filter (library centroid + topic embeddings + recency decay), Stage 2 LLM relevance and novelty scoring on top 50 candidates using the local Ollama fast model, Stage 3 weighted combination with author-match bonus. Rating feedback is collected from day one in the new `pulse_ratings` table; Phase 2 will add a Stage 4 per-user logistic regression classifier consuming that data.
- **"Why this paper?" transparency popover** on every Pulse card, showing matched topics, matched authors, per-signal score breakdown, and the LLM's one-sentence reasoning.
- **Telegram delivery rewrite** — `services/telegram_bot/app/orchestration/research_pulse.py` was gutted from ~164 lines of naive keyword-search-and-notify to ~94 lines of thin delivery over `GET /api/pulse/today`. All scoring/processing logic now lives in the `pulse/` package in paper_ingestion. Inline 👍/👎/💾 rating callbacks wired to `POST /api/pulse/rate`.
- **New database tables (migration 018)** — `pulse_decks`, `pulse_cards`, `pulse_ratings`, `pdf_resolutions`. Plus one new optional column `topics.description TEXT NULL`. Plus new `paper_sources` rows for `openalex` and `pubmed`. Plus new `user_config` entries seeding Pulse defaults (`pulse.enabled`, `pulse.cron`, `pulse.deck_size`, `pulse.stage2_top_k`, `pulse.weights`).
- **New backend package** `services/paper_ingestion/app/pulse/` containing `job.py`, `profile.py`, `discovery.py`, `scoring.py`, `prompts.py`, `deck.py`, `resolver.py`, `__init__.py`.
- **New API router** `services/paper_ingestion/app/routers/pulse.py` exposing six endpoints: `POST /api/pulse/generate`, `GET /api/pulse/today`, `GET /api/pulse/history`, `POST /api/pulse/rate`, `GET /api/pulse/explain/{card_id}`, `GET /api/pulse/stats`.
- **New frontend components** — `PulseDeck` (My Day widget), `PulseCard` (card component), `WhyPopover` (transparency dialog), and a reusable `InfoTooltip` primitive usable across all Settings sections going forward.
- **Settings extensions** — optional `description` field on Topics, Pulse enable/time-picker in Automation, API-key fields and provider tooltips in Sources, scoring weight sliders in the renamed "Pulse & Recommendations" section.
- **New optional environment variables** — `OPENALEX_API_KEY`, `PUBMED_API_KEY`, `UNPAYWALL_EMAIL`. All optional; Pulse runs with graceful degradation for whatever is provided.
- **Phase 1 new dependency** — `lxml` for PubMed XML parsing. `scikit-learn`, `networkx`, and `bertopic` are deferred to Phase 2 to keep the Phase 1 footprint minimal.
- **Graceful degradation** — five failure modes exercised by `test_pulse_degradation.py`: empty profile, all sources disabled, Stage 2 LLM timeout, zero candidates after Stage 1, Qdrant unavailable.
- **Scheduler wiring** — APScheduler gains one new `pulse_overnight` job in `scheduler.py`; no new scheduling mechanism.
- **Eval harness** — `scripts/eval_pulse.py` runs the full scoring pipeline against a labeled 30-paper synthetic set (10 yes / 10 maybe / 10 no). Current run: precision@10 = 100%, no-leakage = 0% (targets: ≥60% and ≤10% respectively). PASS.
- **Playwright E2E** — `frontend/e2e/pulse.spec.ts` covers the happy-path deck render, rating interaction, and "Why this paper?" popover.
- **Testing discipline** — TDD-strict per the brainstorm decision (Axis 2 = C). Tests were written before implementation for every function in `pulse/`. Source plugins tested with recorded offline fixtures.

**Anti-bloat budget (spec §10):** 11 new Python files (8 pulse package + 2 source plugins + 1 new API router) / 4 new frontend files / 1 new migration / 0 new Docker services / 0 new pages. The router file is the one "+1" over the pulse-package ≤10 limit called out in the spec.

**Sprint diff totals:** 78 files changed, 11,234 insertions, 342 deletions across backend, frontend, telegram bot, docs, migrations, and tests.

### Added
- **My Day page**: daily productivity command center with Pomodoro timer, quick-add tasks, project badges, Project Pulse widget
- **Pomodoro timer**: wall-clock based timing with pause/resume, auto-logging of completed sessions, browser notifications, configurable durations
- **Timer settings**: configurable work/break durations and cycle count in Settings page
- **Quick-add tasks**: create tasks from My Day with optional project assignment
- **Project badges**: clickable project labels on tasks linking to Projects page
- **Collapsed empty cards**: Learning/Recommended section collapses to compact row when both empty
- **Recommendation engine** (Phase 1): score-based paper recommendations with liked-paper, project-relevance, and recency signals

### Fixed
- Migration 015 idempotency guard (conditional NOT NULL constraint)
- Migration runner advisory locking (pg_advisory_lock)
- QuickAddTask/TaskList error handling (onError callbacks)
- Vite dev proxy for /api/executive routes
- Backend focus session duration validation (gt=0, le=24)
- Pomodoro auto-logging: completed work sessions now logged automatically
- Timer state persistence: survives page refresh
- Timer accuracy: wall-clock based, no drift in background tabs
- Rate limits on get_my_day (60/min) and log_focus_session (10/min)
- HTTPException inside transaction causing silent rollback in log_focus_session
- QuickAddTaskRequest validation (title 1-500, priority 1-4)
- Recommendations endpoint unbounded limit (now ge=1, le=200)
- Pipeline HTTP timeouts (120-180s for PDF operations)
- Focus command duplicate timer jobs
- Cross-encoder reranker CUDA fallback to CPU
- Reranker lru_cache permanent None caching
- init.sql sync with migrations (paper_recommendations, search_vector)
- Docker learning_engine dependency ordering

## [1.1.0] - 2026-03-08

### Added
- React 19 dashboard replacing Streamlit (Vite + Shadcn/ui + TanStack Query + Zustand)
- Research Feed with filtering, sorting, and paper detail pages
- Citation graph visualization (Cytoscape.js)
- Knowledge graph visualization (Cytoscape.js)
- Structured extraction table with templates
- FSRS spaced repetition for learning cards
- Analytics page with activity, retention, and review charts
- Project management with tasks, milestones, and papers
- Settings page for topics, sources, authors, ingestion, automation, extraction, recommendations
- nginx reverse proxy for frontend routing
- CORS middleware for both FastAPI services
- SSE streaming for paper analysis and RAG chat

## [1.0.0] - 2026-02-15

### Added
- Paper ingestion from arXiv, Semantic Scholar, and manual PDF upload
- Hybrid search (BM25 + semantic) with cross-encoder reranking
- RAG-powered Q&A with citation verification
- Telegram bot with push notifications and daily briefings
- PostgreSQL database with 13 migrations
- Qdrant vector database for semantic search
- LiteLLM for model-agnostic LLM access
- Docker Compose deployment (9 services)
