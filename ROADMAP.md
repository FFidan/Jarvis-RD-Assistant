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

---

## Deferred backlog — refreshed 2026-05-17 (post CI-green + verified-gap-closure program)

CI-Green + Verified-Real Gap Closure shipped to master `a7bfb18f` (GitHub CI GREEN — red since 2026-05-15 now fixed). Genuinely-deferred future work (reflects reality, not memory):

1. **Hermes conversational agent layer** — gate ~2026-05-17; the largest remaining feature; explicitly excluded from the CI-green program.
2. **Performance & hardware-fit** (NEW workstream, own /deep-plan): vLLM-vs-Ollama spike on target high-VRAM hardware *behind the LiteLLM alias abstraction*; perf/memory/GPU profiling of hot paths (Pulse stage-2 scoring, embedding throughput, feed/facet queries, VRAM-residency under concurrent load); per-detected-VRAM default model recommendations + more in-app hardware-fit guidance (no-yaml/env principle). Surfaced because the live embed-smoke was gated by a slow CPU-4B embed on the throwaway dev box.
3. **046/047-class init.sql↔migration-test-harness drift** — `test_migration_046/047` fixed + `live_pg_dsn` connect-retry added this program, but the broader init.sql-snapshot ↔ historical-migration-test pattern remains pre-existing debt.
4. **GUI installer (script-hardening vs desktop GUI)** + **companion docs-site sub-choices** — pre-existing open questions; untouched; still open.
5. **Qdrant corpus re-embed checkpoint** — deliberately NOT done (embed model unchanged); latent/conditional — only if the embedding model is ever changed.

Resolved/moot (NOT deferred): "local-only push" posture (origin == master; GitHub CI is now the oracle); the "other-agent open work" (offline-sync endpoint / caddy crash / micro-deferrals) was verified ALREADY-SHIPPED at `5863ce5f` and excluded for that reason — do not re-flag.

See vault `~/ObsidianVault/projects/JARVIS_RD_Assistant/{open-questions,decisions}.md` 2026-05-17 entries.
