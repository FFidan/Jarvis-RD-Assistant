# JARVIS RD Assistant - Product Requirements Document (PRD)

**Version:** Living document — see git tags and CHANGELOG.md for release versions.
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
| **Unsupported generated claims** | Source-linked retrieval, quote matching, and visible confidence signals |
| **Knowledge decay** | FSRS spaced repetition turns paper insights into durable memory |
| **Poor project tracking** | Lightweight project manager with Telegram milestone reminders |
| **Vendor lock-in / privacy** | Fully self-hosted; LiteLLM supports local models or any API provider |
| **Habit friction** | Push-first UX via Telegram; dashboard for deep dives only |

### Success Definition

A researcher can answer "What papers matter this week, and what should I remember from last
month?" with source-linked evidence and a workflow that makes unsupported output visible.

---

## 2. User Stories

### 2.1 Setup and Configuration
- Install with `./setup.sh`, open its protected finish-setup link, create the
  first administrator, and tune model providers through Settings.
- Add isolated passwordless family or team accounts with passkeys, emailed
  magic links, or administrator-provided one-time links when SMTP is absent.
- Define research topics with search terms and optional descriptions.
- Optionally connect the Telegram bot; set briefing schedules; add or remove
  paper sources.

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

Out of scope: manuscript drafting.

### 3.2 Learning Engine

Auto-generates flashcards from starred/saved papers; each card carries source citation,
evidence quote, and PDF snapshot. FSRS scheduling (`fsrs`); Telegram review sessions;
dashboard card browser, retention stats, Anki export. v2+: cloze, streaks, adaptive limits.

Out of scope: general-purpose flashcard app, collaborative decks, audio/video.

### 3.3 Project Manager

Project CRUD, milestones with due dates, Telegram deadline reminders, paper linking,
`/tasks` and `/done` commands. v2+: Kanban, time tracking, calendar integration.

Projects, milestones, tasks, and their derived output are per-user. Out of
scope: collaborative project ownership, shared decks, and team workflow inside
one project, plus Gantt charts and budget tracking. This boundary does not
exclude family accounts or account isolation: multiple users can sign in to the
same deployment while their project work remains separate.

### 3.4 Zotero Integration

**Shipped:** push and re-sync from JARVIS; manual and scheduled Zotero → JARVIS
sync; personal or group library selection; DOI deduplication; citation-key
copying; and Zotero-highlight import with explicit promotion into verified
notes. Credentials and imported papers are per-user and private to the
connecting user's JARVIS library. Deleting a JARVIS paper does not cascade into
Zotero.

**Planned:** Mendeley import and richer cross-library conflict handling.

### 3.5 Multi-Tenant Auth

JARVIS provides passwordless family and team accounts with account isolation.
The one-time setup token authorizes creation of the first administrator and the
server establishes a row in `sessions` for that browser. Later sign-in uses
magic links or passkeys bound to the exact hostname. A signed-in administrator
can create manual invite and recovery links without SMTP; the operations API
key remains an owner-only recovery path, not a family password. Telegram
pairing is per-user: each account pairs its own chat. `user` and `admin` roles
separate ordinary research work from deployment administration.

Threat model and technical contract: `docs/SECURITY.md` and
`docs/ARCHITECTURE.md`.

---

## 4. Non-Functional Requirements

### 4.1 Security
- Database-backed provider, SMTP, Telegram, source, and Zotero credentials are
  encrypted at rest under `JARVIS_CONFIG_KEY`; host Docker secrets remain a
  separate configuration layer.
- No telemetry, no external analytics, and no third-party font/CDN fetches. Only outbound connections to configured APIs and source integrations.
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
Telegram supports briefings, review prompts, and selected quick actions. The
dashboard is required for setup, administration, PDF reading and annotation,
and evidence-rich research views. It is desktop-optimized and tablet-usable.
English only in v1.

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

### 5.3 Evidence-Grounding Pipeline
1. **Grounded Generation** — LLM receives only paper chunks; metadata from API only.
2. **Quote Verification** — two complementary bars apply. Verbatim quotes
   (summaries, flashcards, extraction) require a 97% fuzzy match
   (`jarvis_common/verify.py` `FUZZY_THRESHOLD`). Synthesized RAG answers
   use a sentence-level grounded-support bar of 70% (`rag/verification.py`
   `RAG_SUPPORT_FUZZY`). These are engineering thresholds, not estimates of
   scientific truth or model accuracy.
   The two bars serve different semantics: 97 is a verbatim-quote check;
   70 is a paraphrase-grounding check. Do not unify them.
3. **PDF Page Snapshots** — 150 DPI pypdfium2; `GET /api/snapshots/{paper_id}/{page}`.
4. **Cross-Reference Check** — semantic consistency checking across ingested papers.

The pipeline assesses support in retrieved text. It does not independently
fact-check a paper, reproduce an experiment, or guarantee that retrieval found
all relevant evidence. See [Methods and limitations](METHODS_AND_LIMITATIONS.md).

### 5.4 Confidence Signals
- **HIGH** — clear abstract, explicit results, all quotes verified
- **MEDIUM** — vague abstract or boundary topic
- **LOW** — multiple quotes failed; deliver raw abstract

### 5.5 User Verification Mechanisms
Tappable citation links; "View Evidence" PDF snapshot button; flag emoji (excluded from
flashcard generation).

### 5.6 Design Principle
**Never sacrifice verification quality for speed.** Pulse cards are *discovery pointers*.
Once a Pulse-sourced paper is saved and processed, the full 4-layer pipeline applies.

---

## 6. Roadmap

### 6.1 Personalization and citation signals (shipped)
Per-user feedback can train a classifier once enough ratings exist. Pulse can
combine classifier output with citation graph signals, and Analytics exposes a
Missing Foundational Papers view. These signals degrade independently when
their data or optional dependencies are unavailable.

### 6.2 Citation-manager next steps (planned)
Mendeley import and richer cross-library conflict handling. Personal and group
Zotero libraries, scheduled pulls, and highlight synchronization are already
shipped.

### 6.3 Advanced retrieval
Cross-paper Ask, citation-graph exploration, tracked-author discovery, and
foundational-paper suggestions are shipped. Multi-round synthesis and
metadata-aware embeddings remain aspirational.

### 6.4 Conversational control (conditional)
A future natural-language control surface may call the existing JARVIS API,
while the services remain authoritative and generated claims keep the same
evidence checks as every other surface. This work requires a separate
performance and safety decision; see the [project roadmap](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/ROADMAP.md).

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

1. Install with `./setup.sh` and complete the first-run web wizard.
2. Configure topics, sources, optional SMTP, optional Telegram, and optional cloud providers through Settings.
3. Discover or import papers and build a daily Pulse with cited summaries.
4. Save, star, summarize, extract, and ask cited questions over papers from the dashboard.
5. Review generated flashcards with FSRS scheduling.
6. Create projects with milestones, notes, and research tasks.
7. View briefing history, card stats, project status, and system health on the dashboard.
8. Verify every generated claim by following citations, quotes, and source-paper links.
