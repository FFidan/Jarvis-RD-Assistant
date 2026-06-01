# Roadmap

This is a living document. Items under **Planned** are directional, not commitments,
and have no fixed dates. See [CHANGELOG.md](CHANGELOG.md) for what has actually
shipped per release, and [docs/SECURITY.md](docs/SECURITY.md) for the security model.

---

## Shipped

Current release: **v0.5.0**.

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
- **Documentation site + user guide.** A published companion site with
  operator/developer docs and an end-user manual covering install through every
  capability (ingestion, hybrid RAG, citation & knowledge graph, contradiction
  detection, Pulse, FSRS flashcards, notes, projects, Telegram, the multi-tenant
  model). The in-repo Markdown remains the offline source of truth.

## In progress

- **Tablet / PWA reading-experience polish.** Refinements to offline reading,
  installable PWA affordances, and tablet layout optimizations for the already-shipped
  information-architecture redesign so already-processed material (summaries,
  extractions, notes) and flashcard review work seamlessly offline.

## Planned (exploratory — no dates)

- **Hermes — conversational layer.** A chat-native interface over the existing
  RAG / citation / contradiction / knowledge-graph substrate: ask your library
  questions and get cited, contradiction-aware answers. **Conditional GO** —
  sequenced after cross-service auth hardening lands, a performance profiling
  pass (to isolate any LLM fan-out regressions), and an evaluation harness
  as the first task (canned research-question set + expected tool sequences,
  so the agent is testable from day one). The orchestration substrate
  (thin custom loop vs an existing agent framework) is an explicit spike —
  the plan does not pre-assume any particular library. Realistic estimate ~20d.

## Not planned

- Per-tenant private *corpus* partitioning. The corpus is intentionally a
  shared scholarly library; isolation is at the activity/output layer. See the
  SECURITY.md "Data Sharing Boundary" for the rationale.

---

*Priorities may shift based on user feedback. Nothing here is a guarantee.*
