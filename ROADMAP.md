# Roadmap

This is a living document. Items under **Planned** are directional, not commitments,
and have no fixed dates. See [CHANGELOG.md](CHANGELOG.md) for what has actually
shipped per release, and [docs/SECURITY.md](docs/SECURITY.md) for the security model.

---

## Shipped

- **Safer service boundaries and recovery (v1.2.6).** The gateway and Telegram
  hold narrowly scoped Platform assertions, Platform, Research, Learning, and
  Operations own their database schemas separately, and restore is a
  host-started operation rather than a browser action.
- **Source-aware visibility and library operations (v1.2.0).** A persisted
  source-aware paper-visibility model that every read path shares, whole-library
  processing, incremental discovery with optional automatic summaries, per-paper
  Markdown knowledge export, visible Pulse ranking-model status, repair for stale
  or missing embeddings, complete PDF-aware restore sets, and explicit instance
  ownership.
- **Safer install and maintenance (v1.1.3).** Setup re-runs preserve local
  configuration and data, access-mode output matches the route that is actually
  served, and the `jarvis-research` command provides checked updates, health
  diagnostics, bounded repair, and a contained uninstall flow.
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
- **Deduplicated papers + private workspaces.** JARVIS avoids duplicating a
  paper that multiple users save while keeping library state and intellectual
  output private. Current visibility details live in the SECURITY.md
  source-aware matrix.
- **Researcher-grade model plane + trust polish.** Per-hardware model selection
  that actually takes effect, optional admin-wide cloud providers whose own model
  lists are fetched at runtime and offered for the `smart` and `fast` roles — with
  the built-in catalog as the offline fallback and the Settings page saying so
  whenever a list cannot be fetched — full-coverage summaries with verified
  quotes, and a reliability/comprehensibility pass: plain-language settings (no
  implementation jargon on screen), honest degraded states with a cause and a
  next step, account data export from Settings, and no silent settings
  dead-ends.
- **Documentation site + user guide.** A published companion site with
  operator/developer docs and an end-user manual covering install through every
  capability (ingestion, hybrid RAG, citation & knowledge graph, contradiction
  detection, Pulse, FSRS flashcards, notes, projects, Telegram, the multi-tenant
  model). The in-repo Markdown remains the offline source of truth.

## In progress

- **Optional diagnostics.** Keeping correlation and bounded health signals
  available without any of them becoming a required external service.
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
  information-architecture redesign. Already processed summaries and notes are
  available offline; a rating for an already-open card can queue for later,
  while loading the next card still needs a connection.

## Planned (exploratory — no dates)

- **Broader knowledge export.** Future releases may add export bundles for
  answers and project-centred outputs.
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

- Public self-registration or a single family-wide password. Accounts are
  invited by an administrator, and recovery stays bound to a user or the
  configured instance owner.

---

*Priorities may shift based on user feedback. Nothing here is a guarantee.*
