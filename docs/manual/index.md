<!-- verified-against-UI: 2026-05-18 | routes: app-wide -->

# JARVIS RD Assistant — User Manual

JARVIS RD Assistant is a **self-hosted AI research assistant** designed for individuals and small teams who want to own their research workflow without relying on external SaaS platforms.

## What it does

- **Ingest papers** from arXiv, Semantic Scholar, Zotero, and other configured sources into your private library.
- **Hybrid RAG chat** — ask questions across your entire library using a retrieval-augmented generation pipeline that combines BM25 keyword search, vector semantic search, and cross-encoder reranking.
- **Citation and knowledge graphs** — explore how papers cite each other and how concepts connect across your corpus.
- **Contradiction detection** — surface papers that make conflicting claims about the same topic.
- **Pulse recommendations** — a daily deck of paper recommendations generated from your active research topics on a configurable schedule.
- **FSRS spaced-repetition cards** — review key facts extracted from papers on a scientifically-spaced schedule.
- **Notes and projects** — annotate papers and organise related work into named projects.
- **Telegram integration** — receive Pulse digests and interact with your library from a Telegram bot.
- **Multi-user** — the system supports multiple accounts with role-based access (regular users and admins).

## Who this manual is for

This manual is for **end users** — researchers and readers who use the web application. It covers every page in the UI, the sign-in flow, and the day-to-day research workflows.

**Operators** (people who install and maintain the system) should also read:

- [`DEPLOYMENT.md`](../DEPLOYMENT.md) — installation, Docker Compose configuration, environment variables, and TLS setup.
- [`SECURITY.md`](../SECURITY.md) — hardening checklist, secret management, and known residual risks.

These documents are part of the repository and are available in the **Get Started** and **Operate** sections of this site.

## Manual organisation

| Page | What it covers |
|------|---------------|
| [Getting Started](getting-started.md) | First-run operator bootstrap, signing in, post-login setup wizard, and the onboarding tour |
| [Navigation](navigation.md) | AppShell layout, sidebar nav groups, TopBar controls (⌘K, Jobs, Pomodoro, theme) |
| [Home & My Day](home-my-day.md) | Home dashboard, My Day page |
| [Research Feed & Library](research-feed.md) | Inbox, Library, Discover (search), and Trash views of the feed |
| [Paper Detail](paper-detail.md) | Three-pane paper view: metadata, full text, RAG chat |
| [Ask (Cross-paper RAG)](ask.md) | Ask questions that span your entire library |
| [Pulse Deck](pulse.md) | Daily recommendation deck, card rating, and schedule configuration |
| [Projects](projects.md) | Organising papers into named projects |
| [Knowledge Graph](knowledge-graph.md) | Interactive concept-level knowledge graph |
| [Citation Graph](citation-graph.md) | Paper-level citation network explorer |
| [Extraction Table](extraction-table.md) | Structured data extracted from papers |
| [Learning Cards](learning-cards.md) | FSRS spaced-repetition review and card library |
| [Analytics](analytics.md) | Reading activity charts and corpus statistics |
| [Settings](settings.md) | Sources, topics, automation, integrations, and account |
| [Telegram](telegram.md) | Pairing your Telegram account and (admin) bot-token configuration |
| [Admin Pages](admin.md) | User management, system health, audit log, backups, system logs (admin role only) |

## How this manual is maintained

Each page in this manual begins with an HTML comment of the form:

```
<!-- verified-against-UI: YYYY-MM-DD | routes: /path1, /path2 -->
```

This records which routes the page was last verified against and when. When the UI changes, the per-page date is bumped and any inaccurate content is corrected. If a date is more than a few months old, treat the page as a starting point and check the live UI for details.

Where a screenshot would be helpful, pages contain placeholder comments of the form:

```
<!-- screenshot: <brief description of what to capture> -->
```

These placeholders make it cheap to add real screenshots later without leaving broken image references in the meantime.
