# Roadmap

This is a living document. Items under **Planned** are directional, not commitments,
and have no fixed dates. See [CHANGELOG.md](CHANGELOG.md) for what has actually
shipped per release, and [docs/SECURITY.md](docs/SECURITY.md) for the security model.

---

## Shipped

Current release: **v1.1.2**. See [CHANGELOG.md](CHANGELOG.md) for the full
change list since the v1.0.0 public baseline.

- **Install from prebuilt images (v1.1.0).** A default `./setup.sh` pulls
  multi-architecture application images from the container registry instead of
  building them locally, ending the multi-gigabyte PyTorch/CUDA build that could
  exhaust disk on a first install; contributors keep a `--build-local` path. The
  same release adds passkey sign-in, rolling 30-day sessions, a hardware-honest
  disk preflight and GPU-vendor detection, a self-explanatory access-mode chooser
  (localhost / LAN / Cloudflare Tunnel / Let's Encrypt), and a staged,
  self-healing restore with browser-driven, off-host disaster recovery.
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
- **Researcher-grade model plane + trust polish.** Per-hardware model selection
  that actually takes effect, optional admin-wide cloud providers for the `smart`
  and `fast` roles, full-coverage summaries with verified quotes, and a
  reliability/comprehensibility pass: plain-language settings (no implementation
  jargon on screen), honest degraded states with a cause and a next step, account
  data export from Settings, and no silent settings dead-ends.
- **Documentation site + user guide.** A published companion site with
  operator/developer docs and an end-user manual covering install through every
  capability (ingestion, hybrid RAG, citation & knowledge graph, contradiction
  detection, Pulse, FSRS flashcards, notes, projects, Telegram, the multi-tenant
  model). The in-repo Markdown remains the offline source of truth.

## In progress

- **First-hour local-first polish.** Tighten the early experience so a fresh
  install is more honest and immediately useful: first-party assets only, clearer
  library-preparation actions, current public version wording, and an onboarding
  path that helps users discover papers before the dashboard feels empty.
- **Local model and retrieval quality validation.** Candidate local model and
  reranking defaults are being evaluated against JARVIS-specific retrieval,
  citation, latency, and structured-output behavior before any public default
  changes. The default product posture remains local-first; cloud providers are
  optional integrations, not requirements.
- **Tablet / PWA reading-experience polish.** Refinements to offline reading,
  installable PWA affordances, and tablet layout optimizations for the already-shipped
  information-architecture redesign so already-processed material (summaries,
  extractions, notes) and flashcard review work seamlessly offline.

## Planned (exploratory — no dates)

- **Day-one library processing.** A clearer whole-library workflow that can
  download, process, summarize, extract, and show progress for a real imported
  or discovered corpus without requiring users to understand the internal
  processing stages.
- **Knowledge export.** Markdown-friendly exports for generated research
  knowledge and project-centered research outputs.
- **Per-user provider keys and routing.** Optional bring-your-own-key behavior,
  per-user routing preferences, and privacy controls for multi-user deployments.
  Current provider settings are admin-wide.
- **Scientific task routing.** Role-aware routing beyond `smart` and `fast`, so
  extraction, synthesis, contradiction checks, card generation, and freshness
  checks can use different local or cloud backends with visible provenance.
- **Cloud budget and privacy guardrails.** Cost estimates, spending caps, and
  provider-use policies for unpublished PDFs, notes, and router providers.
- **Web and academic freshness checks.** Perplexity/Sonar-like search-backed
  providers should be evaluated as explicit freshness tools, not normal private
  library LLM routes.
- **Learning and project workspace evolution.** Better flashcard quality, review
  momentum, project-centered queues, and research-work management should mature
  before the broad conversational layer depends on that context.
- **Hermes — conversational layer.** A chat-native interface over the existing
  RAG / citation / contradiction / knowledge-graph substrate: ask your library
  questions and get cited, contradiction-aware answers. This follows the
  library-value, learning, and project-workspace lanes so the conversation layer
  depends on mature, inspectable context rather than becoming the first place
  core workflows are introduced.
- **Research-workflow expansion.** Longer-horizon exploration of systematic
  review workflows, a JARVIS MCP surface for external research tools, and
  graph-assisted retrieval over the existing citation and knowledge-graph
  substrate.

## Not planned

- Per-tenant private *corpus* partitioning. The corpus is intentionally a
  shared scholarly library; isolation is at the activity/output layer. See the
  SECURITY.md "Data Sharing Boundary" for the rationale.

---

*Priorities may shift based on user feedback. Nothing here is a guarantee.*
