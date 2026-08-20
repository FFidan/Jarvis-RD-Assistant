<!-- verified-against-UI: 2026-08-19 | routes: app-wide -->

# User guide

This guide covers the web application after an operator has installed JARVIS.
It is written for individual researchers, families, and small teams sharing one
self-hosted instance.

## What it does

- Add papers from configured sources or by URL, DOI, title, or upload.
- Search and ask questions across analyzed papers with linked citations.
- Review citation and knowledge graphs and possible contradictions.
- Receive scheduled Pulse recommendations for research topics.
- Create and review spaced-repetition cards, notes, and projects.
- Optionally connect a Telegram bot.
- Use separate user and administrator accounts on a shared instance.

## Who this manual is for

This guide is for researchers, family members, and small teams using an existing
JARVIS instance. Operators should start with the [installation
guide](../readme-shim.md), then return here for first sign-in and account setup.

**Operators** (people who install and maintain the system) should also read:

- [`DEPLOYMENT.md`](../DEPLOYMENT.md) — installation, the five access choices,
  Docker Compose configuration, and operations.
- [`SECURITY.md`](../SECURITY.md) — hardening checklist, secret management, and known residual risks.

These documents are part of the repository and are available in the **Get Started** and **Operate** sections of this site.

## Manual organisation

| Page | What it covers |
|------|---------------|
| [Quick start](quickstart.md) | The one-screen happy path: install, create an admin, and analyze your first paper |
| [First sign-in and setup](getting-started.md) | Create the first administrator, invite family, and add the first paper |
| [Access from other devices](access-modes.md) | Choose localhost, private HTTPS, LAN diagnostics, Cloudflare, or your own domain |
| [Passkeys](passkeys.md) | Add, remove, use, and recover from passkeys with or without email |
| [Navigation](navigation.md) | The simple and full sidebars, the three search inputs, jobs, and the focus timer |
| [Home & My Day](home-my-day.md) | The focused daily page, and the Home dashboard |
| [Papers & Discover](research-feed.md) | Inbox, saved papers, Discover, and Trash |
| [Paper Detail](paper-detail.md) | The reading view: summaries, evidence anchors, related work, notes, and per-paper chat |
| [Ask (Cross-paper RAG)](ask.md) | Ask questions that span your entire library |
| [Pulse Deck](pulse.md) | Daily recommendation deck, card rating, and schedule configuration |
| [Projects](projects.md) | Organising papers into named projects |
| [Knowledge Graph](knowledge-graph.md) | Interactive concept-level knowledge graph |
| [Citation Graph](citation-graph.md) | Paper-level citation network explorer |
| [Extraction Table](extraction-table.md) | Structured data extracted from papers |
| [Learning Cards](learning-cards.md) | FSRS spaced-repetition review and card library |
| [Analytics](analytics.md) | Reading activity charts and corpus statistics |
| [Settings](settings.md) | Account and appearance, sources, topics, automation, and integrations |
| [What your hardware gets you](hardware-and-models.md) | How detected hardware maps to model recommendations |
| [Hardware support matrix](hardware-support-matrix.md) | Supported GPU vendors and acceleration status |
| [Telegram](telegram.md) | Pairing your Telegram account, the commands, and the admin bot token |
| [Backup and restore](backup-and-restore.md) | Keep off-site restore points and recover the instance (admin) |
| [Admin and multi-user operation](admin.md) | Invite users, manage access, health, backups, and logs (admin only) |
