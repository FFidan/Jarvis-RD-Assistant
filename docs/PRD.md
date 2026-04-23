# JARVIS RD Assistant - Product Requirements Document (PRD)

**Version:** 1.3
**Date:** 2026-04-17
**Status:** Active

> Implementation status note (2026-04-10):
> This PRD has been updated to reflect features shipped through v1.2 and to
> embed the approved architectural design for the Discovery & Pulse subsystem
> (Phase 1 planned). Section 3.1 has been rewritten to reflect two distinct
> sub-features (Pulse = discovery, Weekly Summary = reflection). Section 3.4
> tracks features delivered beyond the original MVP scope. Section 8 records
> the Phase 1/2/3 roadmap for the discovery layer. For current technical
> requirements, see `docs/REQUIREMENTS.md`.

---

## 1. Product Vision and Problem Statement

### Vision

JARVIS RD Assistant is an open-source, self-hosted AI-powered research assistant that
delivers curated, citation-backed research briefings, enforces long-term knowledge
retention through spaced repetition, and provides lightweight project management --
all accessible via push notifications on Telegram and a React web dashboard.

### Target Persona

**Early-career researcher (PhD student or postdoc)**

- Tracks 3-8 research topics across 2-3 active projects
- Reads (or should read) 5-15 papers per week
- Uses Telegram daily; checks email/Slack sporadically
- Has access to a personal server, VPS, or university compute node
- Comfortable with Docker but does not want to maintain complex infrastructure
- Distrusts pure AI summaries due to past hallucination experiences
- Procrastinates on reading backlogs; crams before deadlines

### Problems Solved

| Problem | How JARVIS Addresses It |
|---|---|
| **Information overload** -- 50,000+ papers/month on arXiv alone | Automated, filtered daily/weekly briefings scoped to user-defined topics |
| **Hallucination risk** -- LLM summaries fabricate claims and citations | Every claim linked to specific paper sections with exact quotes; 4-layer verification pipeline |
| **Knowledge decay** -- read-and-forget cycle | Spaced repetition engine (FSRS) turns paper insights into durable memory |
| **Poor project tracking** -- scattered notes, missed deadlines | Lightweight project manager with milestone reminders via Telegram |
| **Vendor lock-in / privacy** -- proprietary tools see unpublished work | Fully self-hosted; LiteLLM lets you use local models or any API provider |
| **Habit friction** -- too lazy to maintain Anki, open dashboards | Push-first UX: everything comes to you via Telegram; dashboard is for deep dives only |

### Success Definition

JARVIS is successful when a researcher can answer: "What important papers were published
in my field this week, and what should I remember from last month?" -- without opening a
browser, without hallucinated claims, in under 2 minutes of reading.

---

## 2. User Stories

### 2.1 Setup and Configuration

- **US-001:** As a researcher, I want to deploy JARVIS with a single `docker compose up` command so that I do not spend hours on infrastructure.
- **US-002:** As a researcher, I want to configure my LLM provider (OpenAI, Anthropic, local Ollama) through environment variables so that I am not locked into any vendor.
- **US-003:** As a researcher, I want to define my research topics with search terms so that JARVIS knows what to track.
- **US-004:** As a researcher, I want to connect my Telegram account by entering a bot token so that I receive push notifications.
- **US-005:** As a researcher, I want to set my preferred briefing schedule so that updates arrive when I am ready to read them.
- **US-006:** As a researcher, I want to add or remove paper sources so that JARVIS covers the venues I care about.

### 2.2 Research Pulse Module

- **US-101:** As a researcher, I want to receive a daily briefing on Telegram summarizing new papers matching my topics so that I stay current without manual searching.
- **US-102:** As a researcher, I want each paper summary to include the title, authors, venue, date, a 2-3 sentence summary, and a direct link to the full paper so that I can quickly assess relevance.
- **US-103:** As a researcher, I want every factual claim in a summary to be accompanied by an exact quote and page number from the source paper so that I can verify accuracy.
- **US-104:** As a researcher, I want JARVIS to flag when it has low confidence in a summary so that I know when to read the original myself.
- **US-105:** As a researcher, I want to reply to a briefing on Telegram to get an extended summary of a specific paper so that I can dive deeper without leaving the chat.
- **US-106:** As a researcher, I want to star/bookmark a paper from Telegram so that it is saved to my reading list.
- **US-107:** As a researcher, I want to search my past briefings by keyword or date on the dashboard so that I can find a paper I vaguely remember.
- **US-108:** As a researcher, I want to see contradictions between papers flagged automatically so that I notice conflicting claims across my reading.

### 2.3 Learning Engine Module

- **US-201:** As a researcher, I want JARVIS to automatically generate flashcards from papers I have bookmarked so that I retain key findings without manual card creation.
- **US-202:** As a researcher, I want flashcards to include the source paper citation and a link so that I can always trace a fact back to its origin.
- **US-203:** As a researcher, I want to receive spaced repetition review prompts on Telegram at optimal intervals so that I retain knowledge long-term.
- **US-204:** As a researcher, I want to rate my recall directly in Telegram so that the scheduling algorithm adapts to my actual retention.
- **US-205:** As a researcher, I want to see my retention statistics on the dashboard so that I can track my learning progress.
- **US-206:** As a researcher, I want to edit or delete auto-generated flashcards on the dashboard so that I can correct errors or remove irrelevant cards.
- **US-207:** As a researcher, I want to manually create flashcards from the dashboard so that I can add knowledge from sources outside JARVIS.

### 2.4 Project Manager Module

- **US-301:** As a researcher, I want to create a project with a name, description, and deadline on the dashboard so that I can track my active work.
- **US-302:** As a researcher, I want to add milestones with due dates to a project so that I break large goals into manageable steps.
- **US-303:** As a researcher, I want to receive Telegram reminders before a milestone is due so that I do not miss deadlines.
- **US-304:** As a researcher, I want to link bookmarked papers to a project so that my reading list is organized by project context.
- **US-305:** As a researcher, I want to see a project overview on the dashboard showing progress, linked papers, and upcoming milestones.
- **US-306:** As a researcher, I want to quickly check project status via Telegram so that I can get updates on the go.

### 2.5 Cross-cutting / Telegram Interactions

- **US-401:** As a researcher, I want a `/help` command in Telegram that lists all available commands so that I can discover features.
- **US-402:** As a researcher, I want a morning briefing combining paper digest, due flashcards, and task overview so that I start each day informed.
- **US-403:** As a researcher, I want all Telegram interactions to respond within 10 seconds for simple queries so that the experience feels conversational.

### 2.6 Discovery & Pulse (Shipped — Phase 1, 2026-04-11)

- **US-601:** As a researcher, I want JARVIS to proactively discover new papers from external sources overnight so that I wake up to a curated reading list without having to search.
- **US-602:** As a researcher, I want my morning Pulse deck to contain 5-10 curated papers (not 50) so that I can review it in under 10 minutes.
- **US-603:** As a researcher, I want each Pulse card to explain *why* it was selected so that I trust the ranking and learn what the system understood about my interests.
- **US-604:** As a researcher, I want to rate Pulse cards with 👍 / 👎 / 💾 buttons so that tomorrow's picks reflect what I actually care about.
- **US-605:** As a researcher, I want Pulse to run with whatever external API keys I have provided (including none) so that I am never blocked by a paid-API requirement.
- **US-606:** As a researcher, I want Pulse to add an optional free-text description to each topic so that the LLM relevance scorer has semantic context beyond keywords.
- **US-607:** As a researcher, I want Pulse and the existing Weekly Summary to stay complementary — Pulse tells me what to read, Weekly Summary reflects on what I actually read — so that the two features never duplicate each other.
- **US-608:** As a researcher, I want to save a Pulse card into my reading queue with one tap so that I can download and study it on my own schedule.
- **US-609:** As a researcher, I want a Pulse History view so that I can look back at what was surfaced in past weeks and spot recurring topics I am ignoring.
- **US-610:** As a researcher, I want Pulse to deliver its morning card deck to both the My Day page and optionally Telegram so that the channel matches where I start my day.

---

## 3. Feature Specifications (MVP)

### 3.1 Research Pulse Module

The Research Pulse module combines two complementary sub-features: **Pulse (Discovery)** — a proactive overnight paper discovery layer — and **Weekly Summary (Reflection)** — a retrospective per-topic digest of papers the user actually engaged with. They interact as a handoff (discovery surfaces papers → engaged papers feed reflection) without overlap.

#### 3.1.1 Pulse — Proactive Discovery (SHIPPED Phase 1, 2026-04-11)

A ChatGPT-Pulse-inspired subsystem that discovers new papers from external sources while the user sleeps, scores them against the user's research interests, and delivers a small curated card deck each morning.

**Core (Phase 1):**
- Overnight scheduled job (default 04:00) polls enabled external sources in parallel.
- Phase 1 sources: arXiv (existing plugin, extended), Semantic Scholar (existing, extended), OpenAlex (new plugin, free key required), PubMed (new plugin, enabled by default, optional key for rate limit upgrade).
- Hybrid scoring pipeline: Stage 1 embedding similarity to library centroid + topic embeddings with recency decay → Stage 2 LLM relevance and novelty scoring (local Ollama fast model) on top 50 candidates → Stage 3 weighted combination with author-match bonus.
- Morning delivery at configurable time (default 08:00) as a small card deck (5-10 cards) via the My Day page widget and optional Telegram message.
- Lightweight feedback buttons per card: 👍 like, 👎 dismiss, 💾 save to reading queue, 📖 open paper detail. Ratings persisted to a new `pulse_ratings` table.
- "Why this paper?" transparency popover on every card, displaying matched topics, matched authors, per-signal scores, and the LLM's one-sentence reasoning.
- Ephemeral UX: today's deck shows in the main widget; after 24 hours it rolls into Pulse History tab on the Research Feed.
- Graceful degradation: Pulse runs with any subset of sources enabled. No keys required for baseline operation (arXiv + PubMed both ship enabled by default).
- PDF resolution chain (arXiv direct → Unpaywall fallback) triggered lazily when a card is saved, to obtain free legal PDFs for paywalled papers.
- Rating data is collected silently from Phase 1; Phase 2 activates a per-user logistic-regression classifier that consumes the accumulated ratings as a rescoring layer.

**Architectural footprint (Phase 1):**
- New backend package `services/paper_ingestion/paper_ingestion/pulse/` contains all Pulse logic: `job.py` (overnight orchestrator), `profile.py` (load user profile), `discovery.py` (parallel source fan-out), `scoring.py` (3-stage pipeline), `prompts.py` (version-controlled LLM system prompt), `deck.py` (deck assembly and persistence), `resolver.py` (PDF resolution chain).
- New source plugins: `services/paper_ingestion/paper_ingestion/sources/openalex_source.py` and `pubmed_source.py`.
- Existing `PaperSource` ABC extended with two optional methods (`fetch_new_since`, `get_recommendations`) that default to empty lists so legacy sources do not need modification.
- New database tables: `pulse_decks`, `pulse_cards`, `pulse_ratings`, `pdf_resolutions`. New optional column: `topics.description`.
- New API router `services/paper_ingestion/paper_ingestion/routers/pulse.py` exposes six endpoints: `POST /api/pulse/generate`, `GET /api/pulse/today`, `GET /api/pulse/history`, `POST /api/pulse/rate`, `GET /api/pulse/explain/{card_id}`, `GET /api/pulse/stats`.
- New frontend components: `PulseDeck` (My Day widget), `PulseCard` (card component matching existing Research Feed card style), `WhyPopover` (transparency dialog), and a reusable `InfoTooltip` primitive (generalized `(i)` info tooltip for Settings).
- Settings extensions: Topics gain optional description field, Automation retains general scheduling controls (Pulse controls moved to dedicated tab), Sources gain API-key fields and provider tooltips. A new dedicated **Pulse** settings tab consolidates enable/schedule/scoring weights/manual generate/diagnostics, replacing the former "Recommendations" tab.
- Existing `services/telegram_bot/telegram_bot/orchestration/research_pulse.py` (~165 lines of naive keyword search + notify) is gutted and rewritten as a ~40-line thin delivery wrapper over `GET /api/pulse/today`.
- APScheduler gains one new job (`pulse_overnight`) in `scheduler.py`; no new scheduling mechanism.

**Anti-bloat commitments (actual on ship 2026-04-11):** 4 new tables + 1 new column, 1 new migration (018), 1 new Python package, 11 new Python source files (8 pulse package + 2 source plugins + 1 new API router), 6 new API endpoints, 4 new frontend files, 0 new Docker services, 0 new pages. All Pulse logic is colocated in `pulse/` and `sources/*_source.py`; no scatter across existing modules. The router file is the "+1" over the pulse-package ≤10 limit called out in the original spec.

#### 3.1.2 Weekly Summary — Retrospective Reflection (existing, renamed)

The former weekly digest feature, renamed from `digest.py` to `weekly_summary.py` to free the "Pulse" name and to clarify its distinct purpose.

**Core (shipped):**
- Weekly LLM-synthesized per-topic digest of papers the user engaged with during the past 7 days.
- Runs weekly (default Monday 09:00) via the existing `/api/digest` endpoint (URL unchanged after rename).
- Delivered via the Research Feed weekly summary section and optional Telegram weekly digest message.

**Engagement filter (narrowed during Phase 1 rename):** the SQL query now explicitly excludes papers that appeared in a Pulse deck but received no engagement (no save, no upvote, no open). Weekly Summary reflects only papers with `user_state IN ('saved', 'reading', 'read')` or with a positive `pulse_ratings` entry in the last 7 days. This is Model C — complementary, zero-overlap with Pulse by construction rather than by convention.

**Complementarity guarantee (Model C):** Pulse and Weekly Summary answer different questions with different temporal stances and different corpora. Pulse is forward-looking ("what should I read today?"), external-corpus, daily, card-shaped. Weekly Summary is backward-looking ("what did my reading this week mean?"), internal-library-only, weekly, narrative-themes-shaped. They share infrastructure (embedder, LLM client, topics, authors) but never duplicate output. A dedicated drift-prevention test in `test_weekly_summary.py` asserts that Pulse-pending papers never leak into Weekly Summary output.

#### 3.1.3 Historical Core (shipped, v1 baseline)

Features shipped in v1 that form the foundation Pulse and Weekly Summary build on:
- Scheduled paper fetching from arXiv and Semantic Scholar APIs.
- Local PDF upload and bulk directory scan.
- Topic matching via embedding similarity (configurable threshold).
- LLM-generated summaries with mandatory inline citations (see Section 5).
- Cross-reference consistency checking between papers (semantic similarity via Qdrant).
- Trend detection via relevance scoring and similarity search.
- Relevance feedback loop (rating 1-5, flagging suspicious summaries).
- Paper Recommendation Engine Phase 1 (liked centroid + project context). This engine survives as one signal among several in the new Pulse scoring pipeline; it is not replaced.
- Tracked authors with author alert orchestration; the `tracked_authors` table and author matching logic are reused verbatim as the author-bonus signal in Pulse scoring.

**Out of scope:**
- Full-text PDF annotation
- Manuscript drafting

### 3.2 Learning Engine

**Core (v1):**
- Auto-generation of flashcards from bookmarked papers
- Each card carries source citation + evidence quote + PDF snapshot
- FSRS-based scheduling (py-fsrs)
- Telegram-based review sessions with recall rating
- Dashboard: card browser, retention stats, manual card CRUD
- Anki export

**Nice-to-have (v2+):**
- Cloze deletion and image-based card types
- Anki import
- Review streaks and gamification
- Adaptive daily review limits

**Out of scope:**
- General-purpose flashcard app
- Collaborative decks
- Audio/video cards

### 3.3 Project Manager

**Core (v1):**
- Project CRUD on dashboard
- Milestones with due dates
- Telegram deadline reminders
- Link papers to projects
- `/tasks` and `/done` Telegram commands

**Nice-to-have (v2+):**
- Kanban board view
- Time tracking
- Calendar integration (Google Calendar, ICS)
- Auto-suggested reading plans

**Out of scope:**
- Multi-user team management
- Gantt charts
- Budget tracking

### 3.4 Shipped Beyond MVP (v1.0 to v1.1)

These features were promoted from v2 or added during development:

- **Conversational RAG**: `POST /api/papers/{id}/ask` -- ask questions about any
  processed paper; answers grounded in paper chunks with source citations
- **Semantic Scholar source**: Full integration with rate limiting and optional API key
- **Local PDF ingestion**: Upload individual PDFs or bulk-scan a directory
- **Automated fetch-embed pipeline**: APScheduler-based (`AUTO_FETCH_INTERVAL_HOURS`)
  discovers new papers, downloads PDFs, extracts text, chunks, and embeds automatically
- **Batch flashcard generation**: `POST /api/generate/batch` -- generate cards for all
  unprocessed papers in a deck with one click
- **Cross-paper similarity**: `GET /api/similar/{paper_id}` -- find semantically related
  papers via Qdrant vector search
- **Relevance scoring**: `POST /api/relevance-score` -- score paper-topic relevance
- **User feedback**: Rating (1-5) and suspicious-summary flagging per paper
- **Full-text search**: PostgreSQL tsvector + GIN index on papers table
- **Shared utility library**: `jarvis_common` -- auth, rate limiting, DB helpers
- **10-page React dashboard**: Home, Research Feed, Paper Detail, Learning Cards,
  Projects, Settings, Analytics, Extractions, Citation Graph, Knowledge Graph

### Shipped Beyond MVP (v1.2.0 to v1.2.4)

These features shipped in the Phase 1 Discovery & Pulse sprint and subsequent audit remediation cycles:

- **Unified Async Job System** (migration 023): `jobs` table in PostgreSQL, `jarvis_common/jobs.py` module with `@job_handler` registry and per-service worker loops, REST endpoints (`POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/stream` SSE, `POST /api/jobs/{id}/cancel`), frontend Zustand `job-store` + TopNav `JobsIndicator` + Sonner toast notifications. Migrated kinds in Phase 1: `pulse.generate`, `paper.process`, `paper.analyze`, `card.generate`, `card.generate_batch`. Phase 2 items (Summarize, Extraction, Batch Process, Scan Local, Citations, Weekly Digest) remain on the roadmap.
- **My Day redesigned as triage dashboard**: `DayHeader` with counters (focus time, tasks, cards due), Pulse preview card (3 top papers + link to full deck at Research Feed → Today's Pulse), Pomodoro + Tasks block, ActionItems (overdue + due-today tasks with Focus buttons), Learning summary, Project summary. Replaces the previous full PulseDeck widget on the My Day page.
- **Global Pomodoro header widget**: compact timer display in the TopBar, visible on every page when a session is active, hidden when idle. Clicking it navigates back to My Day.
- **Source drag-to-reorder**: `paper_sources.display_order` column + `PATCH /api/sources/reorder` endpoint + `@dnd-kit/sortable` grip handle UI in Settings → Sources.
- **Discover multi-source search**: checkbox multi-select over ArXiv, Semantic Scholar, OpenAlex, PubMed with backend fan-out, per-source result deduplication, and per-source error isolation.
- **Pulse Diagnostics**: `GET /api/pulse/debug` endpoint + expandable Diagnostics section in Settings → Pulse tab.
- **Pulse settings tab**: dedicated "Pulse" tab in Settings consolidating enable/schedule/scoring weights/manual generate/diagnostics. Replaces the former "Recommendations" tab. Automation tab no longer carries Pulse controls.
- **Info-bubble tooltips**: hover tooltips on source cards, notification rows, and Paper Detail action buttons for context-sensitive help.
- **Pulse `degraded_reason`**: `pulse_decks.degraded_reason` column (migration 023) distinguishes soft degraded runs (deck produced with fallback scoring) from fatal errors (`last_error`). Frontend shows the appropriate contextual message.
- **PubMed sort fix**: PubMed search relevance sort was previously silently ignored (hardcoded to `pub_date`). Now correctly passes `sort=relevance`.
- **Local-paper analyze fix**: locally-uploaded PDFs skip the download step in "Analyze Paper" instead of erroring with "no PDF URL".
- **Generate Cards error link**: "no processed chunks" error in Generate Cards is now a clickable link to Process PDF.
- **Sidebar label**: "Feed" renamed to "Research Feed" throughout navigation.

### 3.5 Zotero Integration

JARVIS integrates with Zotero as the citation management layer.

**Phase 1 — JARVIS → Zotero push:**
- Auto-push on star: when a paper is starred AND linked to a project, enqueue a Zotero push job
- Manual push via "Send to Zotero" button on Paper Detail
- C1 strict scope: only push papers linked to a project (no "Unsorted" fallback)
- Attach PDF when available
- Match JARVIS topics as Zotero tags; DOI dedupe before push; push-once semantics
- Store Better BibTeX citation key (BBT) via localhost:23119; fall back to Zotero item key if BBT unreachable
- JARVIS delete does NOT cascade to Zotero

**Phase 2 — Zotero → JARVIS sync:**
- Hourly poll of Zotero library (incremental via `?since=<version>`)
- Papers clipped via browser extension auto-ingest into JARVIS
- DOI-based deduplication with existing JARVIS papers

### My Day — Daily Productivity Command Center

The My Day page is the researcher's daily triage hub, redesigned around a morning review workflow:

- **DayHeader**: Summary counters for today's focus time, pending tasks, and cards due, giving an instant status snapshot at a glance.
- **Pulse Preview Card**: Shows the 3 most-relevant papers from today's Pulse deck with a "View full deck" link navigating to Research Feed → Today's Pulse tab. Keeps My Day focused on triage rather than full deck review.
- **Pomodoro + Tasks block**: Wall-clock based 25/5/15 timer with pause/resume, auto-logging of completed sessions, browser notifications, and configurable durations (15-60 min work, 3-15 min short break, 10-30 min long break, 2-8 cycles). Quick-Add Tasks creates tasks from My Day with optional project assignment.
- **ActionItems**: Filtered view of overdue + due-today tasks with project badges and per-task Focus buttons to start a Pomodoro session.
- **Learning Summary**: Cards due count with direct link to review.
- **Project Summary**: At-a-glance view of active project progress (done/total tasks) with next milestone and deadline.
- **Focus Tracking**: Daily focus hours accumulated from Pomodoro sessions, focus streak tracking (consecutive days with focus time).
- **Global Pomodoro widget** (TopBar): when a Pomodoro session is running, a compact timer badge is visible in the navigation bar on every page. Clicking it navigates back to My Day. Hidden when no session is active.

---

## 4. Non-Functional Requirements

### 4.1 Security

- LLM credentials stored only in `.env` or Docker secrets; never logged or transmitted beyond the configured provider.
- Telegram bot validates `chat_id` to prevent unauthorized access.
- No telemetry, no phoning home, no external analytics.
- Only outbound connections to configured APIs. No inbound ports beyond the dashboard.
- LiteLLM API keys support rotation without downtime.

### 4.2 Performance

| Operation | Target |
|---|---|
| Daily briefing generation (10 topics, ~50 papers) | < 5 minutes end-to-end |
| Telegram simple command (`/help`, `/tasks`) | < 3 seconds |
| Telegram conversational query (LLM-backed) | < 15 seconds |
| Flashcard review prompt delivery | < 3 seconds |
| Dashboard page load | < 5 seconds |

### 4.3 Reliability

- Paper source degradation: retry with backoff, proceed with remaining sources, note unavailability in briefing.
- LLM failure: retry 3x, then deliver raw briefing (titles + abstracts only).
- Missed cron triggers: detect on startup and run immediately.
- Idempotency: re-running a briefing for the same date must not produce duplicates.

### 4.4 Accessibility

- Telegram: all core functionality accessible without the dashboard. Messages formatted for small screens.
- Dashboard: optimized for desktop browsers, usable on tablets.
- v1 is English only.

---

## 5. Anti-Hallucination Requirements (CRITICAL)

This is the differentiating feature of JARVIS. Every design decision prioritizes
verifiability over fluency.

> Current implementation note (2026-03-10):
> verified quotes and findings exist in the current system, but summary prose and
> RAG answers should be treated as retrieval-grounded rather than fully
> claim-verified end-to-end. The requirements in this section remain the target
> state the implementation should converge toward.

### 5.1 Citation Rules

1. **No uncited claims.** Every factual statement must cite the source paper.
2. **One source per claim.** Multi-paper claims cite each individually.
3. **Section-level attribution** when full-text is available.
4. **Verbatim over paraphrase** for specific results (numbers, metrics).

### 5.2 Evidence Requirements

Each paper summary must include:
- Title, authors, date, venue -- from source API, never LLM-generated
- Original abstract -- available on request
- LLM summary -- 2-3 sentences, every sentence cited with page number
- Key claims list -- structured: `Claim | Exact Quote | Page Number`
- Direct link to original paper

### 5.3 4-Layer Verification Pipeline

1. **Grounded Generation** -- LLM receives only paper chunks as context; metadata from API only
2. **Quote Verification** -- Every claimed quote verified against source text (exact + fuzzy 92%)
3. **PDF Page Snapshots** -- Highlighted screenshots of cited pages as visual evidence
4. **Cross-Reference Check** -- Consistency checking against other ingested papers

### 5.4 Confidence Signals

- **HIGH** -- clear abstract, explicit results, all quotes verified
- **MEDIUM** -- vague abstract or boundary topic
- **LOW** -- multiple quotes failed verification or highly specialized content
- If all quotes fail: replace with "Unable to summarize reliably" + original abstract

### 5.5 User Verification Mechanisms

- Tappable citation links to original papers
- "View Evidence" button sends highlighted PDF page snapshot
- Flag emoji to mark suspicious summaries (excluded from flashcard generation)
- Audit trail on dashboard: raw prompt, raw chunks, raw LLM response

---

## 6. Success Metrics

### Engagement (after 30 days)

| Metric | Target |
|---|---|
| Briefing interaction rate | > 70% of delivered briefings |
| Papers bookmarked per week | >= 3 |
| Review sessions per week | >= 4 |
| Dashboard visits per week | >= 2 |

### Learning

| Metric | Target |
|---|---|
| Flashcard retention rate (Good/Easy after 7+ days) | > 80% |
| Active card count growth | Positive week-over-week |
| Review streak | >= 5 days/week average |

### Accuracy

| Metric | Target |
|---|---|
| User-flagged inaccuracies | < 5% of summaries |
| Metadata correctness | 100% |
| Citation completeness | 100% of factual sentences cited |

---

## 7. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| arXiv/Semantic Scholar API rate limits or downtime | High | Medium | Caching, backoff, graceful degradation |
| LLM quality variance across providers | Medium | High | Standardized prompts, output validation, tested model configs |
| py-fsrs scheduling edge cases | Low | Medium | Unit tests, fallback to simple intervals |
| n8n integration complexity | Low | Low | n8n is optional (`--profile n8n`); APScheduler handles core scheduling |
| Docker resource consumption on small VPS | Medium | Medium | Document minimum specs (4GB RAM), optional components |
| Users don't trust AI summaries | Medium | High | 4-layer anti-hallucination is core mitigation |
| Briefing fatigue | Medium | Medium | Relevance filtering, configurable frequency |
| Setup too complex | High | High | Clear .env.example, step-by-step README, future setup wizard |

---

## 8. v2 Roadmap

Informed by competitive analysis of Elicit, ResearchRabbit, Semantic Scholar, Connected
Papers, and ChatGPT Pulse.

### 8.1 Competitive Positioning

Our moat: **anti-hallucination verification** (4-layer pipeline with quote matching + PDF
snapshots), **spaced repetition from papers** (FSRS), and — once shipped — **a fully
self-hosted proactive discovery layer** that draws on the best patterns from open-source
research tools (see §8.5 attribution). No single competitor offers all three.

Our previous biggest gap was **cross-paper intelligence** (addressed by cross-paper RAG
with query decomposition). Our current biggest gap is **proactive discovery** — helping
the researcher find papers they did not know existed. The Phase 1 Discovery & Pulse
subsystem (§8.5) directly closes this gap.

### 8.2 Priority Features (from competitor best practices)

**Tier 0 -- Blockers (tool is frustrating without these):**
- ~~"What's New" paper feed with unread markers and relevance ranking~~ DONE
- ~~Cross-paper RAG (query all embedded papers at once)~~ DONE
- ~~Reading priority / triage (must-read / recommended / background badges)~~ DONE

**Tier 1 -- Important (daily-driver quality):**
- ~~Hybrid search: fuse PostgreSQL full-text + Qdrant vectors via reciprocal rank fusion~~ DONE
- ~~Cross-encoder reranking for retrieval quality~~ DONE
- **Weekly digest / research report (Pulse-inspired proactive briefing) — SPEC'D, see §8.5 Phase 1**
- ~~Paper notes and annotations~~ DONE
- ~~Telegram bot activation (push-first UX is the core value proposition)~~ DONE

**Tier 2 -- Polish (feature-competitive):**
- ~~Bigger embedding model (nomic-embed-text or bge-m3 replacing qwen3-embedding:0.6b)~~ DONE
- ~~TLDR one-line summaries (Semantic Scholar-inspired)~~ DONE
- ~~Seed-based paper discovery (ResearchRabbit-style "find more like these")~~ DONE
- ~~Author alerts (track researchers you follow)~~ DONE
- ~~Query decomposition for complex questions~~ DONE
- ~~Streaming LLM responses~~ DONE

**Tier 3 -- Differentiation (tool becomes special):**
- ~~Citation graph visualization (ResearchRabbit/Connected Papers-inspired)~~ DONE
- ~~Structured data extraction tables (Elicit-style custom columns per paper)~~ DONE
- ~~Knowledge graph (entities, methods, datasets extracted from papers)~~ DONE
- ~~React dashboard migration (replace Streamlit)~~ DONE

### 8.3 RAG Quality Targets

| Component | Current (v1.2) | Target (v2) |
|-----------|----------------|-------------|
| Embedding model | nomic-embed-text (768d) | _(achieved)_ |
| Retrieval | Hybrid RRF + cross-encoder rerank | _(achieved)_ |
| Scope | Cross-paper search | _(achieved)_ |
| Generation | Query decomposition | _(achieved)_ |
| Streaming | LiteLLM streaming + SSE | _(achieved)_ |

### 8.4 Design Principle

**Never sacrifice verification quality for speed.** Every new feature (cross-paper RAG,
Pulse discovery, Weekly Summary, and everything else) must preserve the anti-hallucination
guarantees defined in §5. Pulse cards, in particular, surface papers from external sources
before they have been ingested into the library. A Pulse card is a discovery pointer, not
a verified finding. Once a paper is saved from Pulse and processed, the normal 4-layer
verification pipeline applies to any summaries or flashcards generated from it. Pulse
itself does not generate verified claims — it generates *reasons to look at a paper* with
transparent per-signal scoring.

### 8.5 Phase 1 — Discovery & Pulse + Jobs Subsystems (SHIPPED 2026-04-11 / 2026-04-17)

A proactive overnight paper discovery subsystem that complements the existing library
management features. The architectural design is embedded in §3.1.1; this section
captures the roadmap phasing, acceptance criteria, and attribution.

**Unified Async Job System (SHIPPED 2026-04-17):** migration 023 adds the `jobs` table and `jarvis_common/jobs.py` with the `@job_handler` registry and per-service worker loops. `pulse.generate`, `paper.process`, `paper.analyze`, `card.generate`, and `card.generate_batch` are migrated in Phase 1. Phase 2 items: Summarize, Extraction, Batch Process, Scan Local, Citations, Weekly Digest. See §3.4 "Shipped Beyond MVP" for the full Jobs architecture.

#### 8.5.1 Phase 1 (target)

- Core loop: overnight discovery job → hybrid embedding + LLM scoring → morning card deck delivery → feedback capture.
- Sources: arXiv (extend), Semantic Scholar (extend), OpenAlex (new plugin), PubMed (new plugin, enabled by default).
- PDF resolution chain: arXiv → Unpaywall fallback.
- Delivery: My Day widget, Research Feed "Today's Pulse" / "Pulse History" tabs, optional Telegram morning message.
- Feedback: 👍 / 👎 / 💾 / 📖 / dismiss buttons; ratings persisted to `pulse_ratings` table from day one.
- Transparency: "Why this paper?" popover on every card, showing per-signal breakdown and LLM reasoning.
- Rename: `digest.py` → `weekly_summary.py`, with SQL filter narrowed to engaged papers only (Model C non-overlap with Pulse).
- Existing telegram_bot `research_pulse.py` gutted and rewritten from ~164 lines to ~94 lines as a thin delivery wrapper over `GET /api/pulse/today`, with inline 👍/👎/💾 rating callbacks wired to `POST /api/pulse/rate`.

**Acceptance targets (set during the brainstorm session 2026-04-10):**
- Robustness: reliable daily operation for a full week without intervention; all graceful degradation paths tested; observability (per-source candidate counts, Stage 2 LLM call counts and latency, per-signal score breakdowns) logged to the existing JSON log formatter.
- Testing: TDD-strict. Tests written before implementation for every function in the new `pulse/` package. Source plugins tested with recorded offline fixtures.
- Evaluation: a labeled 30-paper eval set (10 yes, 10 maybe, 10 no) with acceptance target of ≥60% of "yes" papers in the top 10 and ≤10% of "no" papers in the top 10. Re-runnable whenever scoring weights or the LLM prompt change.

#### 8.5.2 Phase 2 (deferred, after Phase 1 is daily-driver stable)

- Per-user logistic-regression classifier trained nightly on the `pulse_ratings` table (scikit-learn). Becomes Stage 4 of the scoring pipeline once ≥30 ratings exist. No cold-start problem because Phase 1 collects ratings silently from day one.
- Citation graph scoring signals using the existing T3-1 citation graph: PageRank on the local subgraph surfaces foundational papers; Adamic/Adar link prediction finds papers sharing rare citation partners.
- "Missing Foundational Papers" widget — flags papers that are heavily cited within the user's library but missing from it.
- BERTopic dynamic topic modeling monthly job → "Rising Topics in Your Field" widget.
- CORE added to the PDF resolver chain as a secondary fallback alongside Unpaywall.

#### 8.5.3 Phase 3 (aspirational)

- "Ask the Literature" feature: a separate synthesis-style query path distinct from Pulse polling and from the existing library RAG. Operates on a synthesis-capable source adapter. Users with a Consensus Pro subscription can plug in their key (plugin interface documented; concrete plugin not shipped because we cannot quality-test without active access).
- Multi-round RAG (OpenScholar-style iterative self-feedback loop) as an upgrade to the existing single-pass RAG.
- Metadata-aware embeddings (PaperQA2 pattern) fusing chunk text with paper-level metadata during indexing.
- Auto-populating author watchlist from starred papers.

### 8.6 Zotero Roadmap

- Group library support (`library_type="group"`)
- Zotero annotations import (PDF highlights/notes → JARVIS notes)
- Mendeley integration (analogous design)
- API key encryption at rest in user_config

### 8.7 Inspiration and Prior Art

JARVIS's Discovery & Pulse design borrows ideas and patterns from several open-source and public research tools. These are credited for their intellectual contribution; no code is copied.

- **[ChatGPT Pulse](https://openai.com/index/introducing-chatgpt-pulse/)** (OpenAI) — async overnight research, morning card deck UX, ephemeral delivery, feedback loop pattern.
- **[zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily)** — using the existing library as a preference model via weighted centroid cosine similarity.
- **[GPT Paper Assistant](https://github.com/tatsu-lab/gpt_paper_assistant)** — two-axis LLM scoring (relevance + novelty) and author watchlist via Semantic Scholar author IDs.
- **[ArxivDigest](https://github.com/AutoLLM/ArxivDigest)** — natural-language interest descriptions driving LLM-based relevance ranking over abstracts.
- **[Scholar Inbox](https://scholar-inbox.com)** — per-user logistic regression classifier trained on explicit ratings over embedding vectors (Phase 2).
- **[Inciteful](https://inciteful.xyz)** — citation graph algorithms (PageRank + Adamic/Adar) on local subgraphs for paper discovery (Phase 2).
- **[BERTopic](https://github.com/MaartenGr/BERTopic)** — neural topic modeling with dynamic temporal topics for trend detection (Phase 2).
- **[OpenScholar](https://github.com/AkariAsai/OpenScholar)** (Allen Institute) — iterative self-feedback RAG over scientific literature (Phase 3 inspiration).
- **[PaperQA2](https://github.com/Future-House/paper-qa)** (FutureHouse) — metadata-aware embeddings and agentic retrieval (Phase 3 inspiration).

All are MIT/Apache-licensed. Our use is at the idea/pattern level.
digests, extraction) MUST pass through the anti-hallucination verification pipeline.
This is what differentiates JARVIS from every competitor.

---

## Appendix: MVP Scope Boundary

All 8 MVP items verified complete as of 2026-03-08. See Section 3.4 for features
shipped beyond this scope.

The MVP is complete when a user can:

1. Deploy with `docker compose up`
2. Configure topics and LLM provider via `.env`
3. Receive a daily briefing on Telegram with cited summaries
4. Bookmark a paper from Telegram
5. Review auto-generated flashcards on Telegram with FSRS scheduling
6. Create a project with milestones and receive deadline reminders
7. View briefing history, card stats, and project status on the dashboard
8. Verify every claim by tapping through to the source paper or viewing the PDF snapshot
