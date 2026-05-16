# Roadmap

This is a living document. Items under **Planned** are directional, not commitments,
and have no fixed dates. See [CHANGELOG.md](CHANGELOG.md) for what has actually
shipped per release, and [docs/SECURITY.md](docs/SECURITY.md) for the security model.

---

## Shipped

Current release: **v0.4.1**.

- **Multi-tenant, self-hostable.** Magic-link sessions and an API-key session
  path for single-operator installs; admin role separation; Telegram account
  pairing.
- **Security-hardened for public self-hosting.** Strict cross-tenant data
  isolation, audit log, per-user rate limiting, user-deletion cascade with a
  grace window, GDPR data export, and a documented threat model + data-sharing
  boundary (`docs/SECURITY.md`).
- **Research substrate.** Paper ingestion from multiple sources, hybrid
  (BM25 + semantic) retrieval, cross-paper RAG with citations, citation graph,
  knowledge graph, quote-verified contradiction detection, and the Pulse
  recommender that learns from your ratings.
- **Knowledge retention.** Anki-style spaced-repetition flashcards (FSRS),
  notes, structured extractions, projects, and a daily-intent workflow.
- **Shared canonical corpus + private workspace.** The bibliographic corpus is
  shared across users on an instance; all user activity and intellectual
  output is strictly private (see the SECURITY.md "Data Sharing Boundary").

## In progress

- **Installer / first-run UX.** Guided, OS-aware preflight and a single-user
  vs. multi-user setup choice so non-technical researchers can stand up an
  instance with minimal friction.
- **UI redesign + offline reader.** An information-architecture refresh, with
  an installable PWA so already-processed material (summaries, extractions,
  notes) and flashcard review work offline on a tablet.

## Planned (exploratory — no dates)

- **Hermes — conversational layer.** A chat-native interface over the existing
  RAG / citation / contradiction / knowledge-graph substrate: ask your library
  questions and get cited, contradiction-aware answers. Under evaluation; will
  be announced when scoped.
- **Discovery reliability.** Response caching for external metadata sources to
  reduce rate-limit failures during discovery.
- **Documentation site + complete user guide.** A published companion site
  with operator/developer docs (the in-repo Markdown remains the offline
  source of truth for self-hosters) and a complete end-user manual: from
  install and setup through every capability of the project (ingestion,
  hybrid RAG, citation & knowledge graph, contradiction detection, the
  Pulse recommender, FSRS flashcards, notes, projects, Telegram, the
  multi-tenant model) and a full UI/UX walkthrough. The user-guide portion
  follows the in-progress UI redesign so it ships current rather than stale.

## Not planned

- Per-tenant private *corpus* partitioning. The corpus is intentionally a
  shared scholarly library; isolation is at the activity/output layer. See the
  SECURITY.md "Data Sharing Boundary" for the rationale.

---

*Priorities may shift based on user feedback. Nothing here is a guarantee.*
