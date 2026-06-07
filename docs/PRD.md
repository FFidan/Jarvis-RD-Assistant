# JARVIS RD Assistant - Product Requirements Document (PRD)

**Version:** 0.6.0 (Living document — see git tags for release versions.)
**Status:** Active

> This PRD describes the product vision, user stories, feature scope, and forward roadmap.
> For technical requirements and dependency specs, see `docs/REQUIREMENTS.md`.
> For architecture decisions and the multi-tenant model, see `docs/ARCHITECTURE.md`.

---

## 1. Product Vision and Problem Statement

### Vision

JARVIS RD Assistant is an open-source, self-hosted AI research assistant that delivers
citation-backed briefings, enforces knowledge retention via spaced repetition, and
provides lightweight project management — delivered via Telegram and a React dashboard.

### Target Persona

**Early-career researcher (PhD student or postdoc)** tracking 3–8 topics across 2–3
active projects; reads Telegram daily; distrusts uncited AI summaries; has a reading backlog.

### Problems Solved

| Problem | How JARVIS Addresses It |
|---|---|
| **Information overload** | Automated daily/weekly briefings scoped to user-defined topics |
| **Hallucination risk** | Every claim linked to exact quotes and page numbers; 4-layer verification |
| **Knowledge decay** | FSRS spaced repetition turns paper insights into durable memory |
| **Poor project tracking** | Lightweight project manager with Telegram milestone reminders |
| **Vendor lock-in / privacy** | Fully self-hosted; LiteLLM supports local models or any API provider |
| **Habit friction** | Push-first UX via Telegram; dashboard for deep dives only |

### Success Definition

A researcher can answer "What papers matter this week, and what should I remember from last
month?" — without a browser, without hallucinated claims, in under 2 minutes.

---

## 2. User Stories

### 2.1 Setup and Configuration
- Deploy with `docker compose up`; configure LLM provider via environment variables.
- Define research topics with search terms and optional descriptions.
- Connect Telegram bot; set briefing schedule; add or remove paper sources.

### 2.2 Research Pulse Module
- Receive a daily Telegram briefing on new papers matching my topics; each summary
  includes title, authors, venue, date, 2–3 cited sentences, and a direct link.
- JARVIS flags low-confidence summaries; reply on Telegram for an extended summary.
- Save or star a paper from Telegram (`state='to_read'` / `starred=TRUE`).
- Search past briefings by keyword or date on the dashboard.

### 2.3 Learning Engine Module
- Auto-generate flashcards from starred or saved papers, each with source citation
  and evidence quote.
- Receive spaced repetition review prompts on Telegram; rate recall directly in chat.
- View retention statistics and manage cards on the dashboard.

### 2.4 Project Manager Module
- Create projects with name, description, and deadline; add milestones with due dates.
- Receive Telegram reminders before a milestone is due.
- Link starred or saved papers to a project; view project overview on the dashboard.

### 2.5 Cross-cutting / Telegram
- `/help` lists all commands; morning briefing combines digest + due cards + task overview.
- Simple commands respond within 10 seconds.

---

## 3. Feature Specifications

### 3.1 Research Pulse Module

Full API contract (9 endpoints): `docs/contracts/02-pulse.md`. My Day UI: `docs/manual/home-my-day.md`.

**Pulse — Proactive Discovery:** Overnight job fans out to arXiv, Semantic Scholar, OpenAlex,
and PubMed. A three-stage scoring pipeline (embedding similarity → LLM relevance + novelty →
weighted combination) selects 5–10 cards for morning delivery via the My Day page and optional
Telegram message. Feedback writes to `recommendation_feedback`; save sets
`paper_user_state.state='to_read'`. No paid API key required.

**Weekly Summary:** Monday LLM-synthesized per-topic digest of papers the user actually
engaged with. Excludes unengaged Pulse cards — the two features never duplicate output.

Sources also include local PDF upload, bulk scan, and tracked authors.

Out of scope: full-text PDF annotation, manuscript drafting.

### 3.2 Learning Engine

Auto-generates flashcards from starred/saved papers; each card carries source citation,
evidence quote, and PDF snapshot. FSRS scheduling (`fsrs`); Telegram review sessions;
dashboard card browser, retention stats, Anki export. v2+: cloze, streaks, adaptive limits.

Out of scope: general-purpose flashcard app, collaborative decks, audio/video.

### 3.3 Project Manager

Project CRUD, milestones with due dates, Telegram deadline reminders, paper linking,
`/tasks` and `/done` commands. v2+: Kanban, time tracking, calendar integration.

Out of scope: multi-user team management, Gantt charts, budget tracking.

### 3.4 Zotero Integration

**Shipped:** auto-push on star+project-link; DOI dedupe; push-once; PDF attachment;
BBT citation key fallback. Delete does not cascade.
**Planned:** hourly Zotero → JARVIS sync; browser-clipped paper auto-ingest.

### 3.5 Multi-Tenant Auth

Magic-link sign-in, session cookies, Telegram pairing, admin role separation.
Threat model: `docs/SECURITY.md` and `docs/ARCHITECTURE.md`.

---

## 4. Non-Functional Requirements

### 4.1 Security
- Provider credentials encrypted at rest (`JARVIS_CONFIG_KEY`); Docker secrets supported.
- No telemetry, no external analytics. Only outbound connections to configured APIs.
- Startup validates encrypted config rows before schedulers/workers start.

### 4.2 Performance

| Operation | Target |
|---|---|
| Daily briefing generation (10 topics, ~50 papers) | < 5 minutes end-to-end |
| Telegram simple command | < 3 seconds |
| Telegram LLM-backed query | < 15 seconds |
| Dashboard page load | < 5 seconds |

### 4.3 Reliability
- Source degradation: retry with backoff; proceed with remaining sources.
- LLM failure: retry 3×, then deliver raw briefing (titles + abstracts).
- Missed cron triggers: detect on startup and run immediately.
- Idempotent: re-running a briefing for the same date produces no duplicates.

### 4.4 Accessibility
All core functionality accessible from Telegram without the dashboard.
Dashboard: desktop-optimized, tablet-usable. English only in v1.

---

## 5. Anti-Hallucination Requirements (CRITICAL)

Verifiability over fluency — the differentiating feature. All generated content
(summaries, flashcards, Pulse reasoning, RAG answers) MUST pass through the pipeline below.

### 5.1 Citation Rules
1. **No uncited claims.** Every factual statement cites the source paper.
2. **One source per claim.** Multi-paper claims cite each paper individually.
3. **Section-level attribution** when full-text is available.
4. **Verbatim over paraphrase** for specific results (numbers, metrics).

### 5.2 Evidence Requirements
Each summary includes: title / authors / date / venue from source API (never LLM-generated);
2–3 cited sentences with page numbers; key claims as `Claim | Exact Quote | Page`; paper link.

### 5.3 4-Layer Verification Pipeline
1. **Grounded Generation** — LLM receives only paper chunks; metadata from API only.
2. **Quote Verification** — every claimed quote verified (fuzzy ≥92%);
   `paper_ingestion/verification.py` and `rag/verification.py`.
3. **PDF Page Snapshots** — 150 DPI PyMuPDF; `GET /api/snapshots/{paper_id}/{page}`.
4. **Cross-Reference Check** — semantic consistency checking across ingested papers.

### 5.4 Confidence Signals
- **HIGH** — clear abstract, explicit results, all quotes verified
- **MEDIUM** — vague abstract or boundary topic
- **LOW** — multiple quotes failed; deliver raw abstract

### 5.5 User Verification Mechanisms
Tappable citation links; "View Evidence" PDF snapshot button; flag emoji (excluded from
flashcard generation); raw-prompt audit trail on dashboard.

### 5.6 Design Principle
**Never sacrifice verification quality for speed.** Pulse cards are *discovery pointers*.
Once a Pulse-sourced paper is saved and processed, the full 4-layer pipeline applies.

---

## 6. Roadmap

### 6.1 Intelligent Rescoring (planned)
Per-user classifier on `recommendation_feedback` (≥30 ratings). Citation graph signals
(PageRank + Adamic/Adar). BERTopic topic-trend modeling. "Missing Foundational Papers" widget.

### 6.2 Zotero Phase 2 (planned)
Group library support, annotations import, Mendeley integration.

### 6.3 Advanced Retrieval (aspirational)
"Ask the Literature" synthesis path; multi-round RAG; metadata-aware embeddings;
auto-populated author watchlist from starred papers.

### 6.4 Conversational Agent Layer (planning, conditional)
Natural-language control plane over the JARVIS REST API. Agent-as-client pattern — services
stay authoritative; the agent is never the system of record. Every claim must pass
`QuoteVerifier` (§5). Gated behind a perf-profiling pass; see the [project roadmap](https://github.com/FFidan/Jarvis-RD-Assistant/blob/master/ROADMAP.md).

### 6.5 Inspiration and Prior Art
Discovery & Pulse design borrows ideas from (no code copied; all MIT/Apache-licensed):
[ChatGPT Pulse](https://openai.com/index/introducing-chatgpt-pulse/) (card UX),
[zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily) (library-centroid scoring),
[GPT Paper Assistant](https://github.com/tatsu-lab/gpt_paper_assistant) (two-axis LLM scoring),
[ArxivDigest](https://github.com/AutoLLM/ArxivDigest) (natural-language topics),
[Scholar Inbox](https://scholar-inbox.com) (per-user classifier),
[Inciteful](https://inciteful.xyz) (citation graph),
[BERTopic](https://github.com/MaartenGr/BERTopic) (topic modeling),
[OpenScholar](https://github.com/AkariAsai/OpenScholar) (iterative RAG),
[PaperQA2](https://github.com/Future-House/paper-qa) (metadata-aware embeddings).

---

## Appendix: MVP Scope Boundary

The MVP is complete when a user can:

1. Deploy with `docker compose up`
2. Configure topics and LLM provider via `.env`
3. Receive a daily Telegram briefing with cited summaries
4. Save or star a paper from Telegram
5. Review auto-generated flashcards on Telegram with FSRS scheduling
6. Create a project with milestones and receive deadline reminders
7. View briefing history, card stats, and project status on the dashboard
8. Verify every claim by tapping through to the source paper or viewing the PDF snapshot
