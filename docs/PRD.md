# JARVIS RD Assistant - Product Requirements Document (PRD)

**Version:** 1.1
**Date:** 2026-03-08
**Status:** Active

> Implementation status note (2026-03-10):
> This PRD remains the target-state product document. The current implementation
> does not yet satisfy every target-state requirement described below. For
> current implementation gaps and stabilization priorities, see
> `docs/plans/2026-03-10-codebase-findings.md`. For conservative operator truth,
> see `docs/REQUIREMENTS.md`.

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

---

## 3. Feature Specifications (MVP)

### 3.1 Research Pulse

**Core (v1):**
- Scheduled paper fetching from arXiv and Semantic Scholar APIs
- Local PDF upload and bulk directory scan
- Topic matching via embedding similarity (configurable threshold)
- LLM-generated summaries with mandatory inline citations (see Section 5)
- Daily/weekly briefing delivery to Telegram
- Automated research pulse scheduling (APScheduler + optional n8n)
- Paper bookmarking from Telegram
- Briefing archive on dashboard
- Cross-reference consistency checking between papers (semantic similarity via Qdrant)
- Trend detection via relevance scoring and similarity search
- Relevance feedback loop (rating 1-5, flagging suspicious summaries)

**Nice-to-have (v2+):**
- Additional source plugins (PubMed, IEEE, DBLP)
- Collaborative briefings for lab groups

**Out of scope:**
- Full-text PDF annotation
- Manuscript drafting
- Citation graph visualization
- Reference manager integration (Zotero, Mendeley)

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
| n8n workflow complexity | Medium | Medium | Modular workflows, version-controlled JSON exports |
| Docker resource consumption on small VPS | Medium | Medium | Document minimum specs (4GB RAM), optional components |
| Users don't trust AI summaries | Medium | High | 4-layer anti-hallucination is core mitigation |
| Briefing fatigue | Medium | Medium | Relevance filtering, configurable frequency |
| Setup too complex | High | High | Clear .env.example, step-by-step README, future setup wizard |

---

## 8. v2 Roadmap

Informed by competitive analysis of Elicit, ResearchRabbit, Semantic Scholar, Connected
Papers, and ChatGPT Pulse. Full technical details in
`docs/plans/2026-03-08-v2-roadmap.md`.

### 8.1 Competitive Positioning

Our moat: **anti-hallucination verification** (4-layer pipeline with quote matching + PDF
snapshots) and **spaced repetition from papers** (FSRS). No competitor offers either.

Our biggest gap: **cross-paper intelligence**. Elicit can answer "What do studies say
about X?" across thousands of papers. We can only ask about one paper at a time.

### 8.2 Priority Features (from competitor best practices)

**Tier 0 -- Blockers (tool is frustrating without these):**
- "What's New" paper feed with unread markers and relevance ranking
- Cross-paper RAG (query all embedded papers at once)
- Reading priority / triage (must-read / recommended / background badges)

**Tier 1 -- Important (daily-driver quality):**
- Hybrid search: fuse PostgreSQL full-text + Qdrant vectors via reciprocal rank fusion
- Cross-encoder reranking for retrieval quality
- Weekly digest / research report (Pulse-inspired proactive briefing)
- Paper notes and annotations
- Telegram bot activation (push-first UX is the core value proposition)

**Tier 2 -- Polish (feature-competitive):**
- Bigger embedding model (nomic-embed-text or bge-m3 replacing qwen3-embedding:0.6b)
- TLDR one-line summaries (Semantic Scholar-inspired)
- Seed-based paper discovery (ResearchRabbit-style "find more like these")
- Author alerts (track researchers you follow)
- Query decomposition for complex questions
- Streaming LLM responses

**Tier 3 -- Differentiation (tool becomes special):**
- Citation graph visualization (ResearchRabbit/Connected Papers-inspired)
- Structured data extraction tables (Elicit-style custom columns per paper)
- Knowledge graph (entities, methods, datasets extracted from papers)
- ~~React dashboard migration (replace Streamlit)~~ DONE

### 8.3 RAG Quality Targets

| Component | Current (v1.1) | Target (v2) |
|-----------|----------------|-------------|
| Embedding model | qwen3-embedding:0.6b (768d) | nomic-embed-text or bge-m3 |
| Retrieval | Cosine top-5, threshold 0.3 | Hybrid RRF + cross-encoder rerank |
| Scope | Single paper only | Global cross-paper search |
| Generation | Direct prompt, temp 0.1 | Query decomposition for complex Qs |
| Streaming | None | LiteLLM streaming + dashboard display |

### 8.4 Design Principle

**Never sacrifice verification quality for speed.** Every new feature (cross-paper RAG,
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
