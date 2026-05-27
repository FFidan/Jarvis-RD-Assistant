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
  questions and get cited, contradiction-aware answers. **Conditional GO,
  sequenced** — see the Deferred backlog below for the strict ordering
  (RB-3 → Perf Phase 1 → eval harness → Hermes) and rationale.
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

## Deferred backlog — refreshed 2026-05-17 (sequenced; post CI-green + 2026-05-17 audit)

CI-Green + Verified-Real Gap Closure shipped to master `a7bfb18f` (GitHub CI GREEN).
This is a **sequenced** backlog: each item carries its decision and the one-line *why*,
because several were flat "open questions" that have since been decided.

**Active — handed off this session (in flight):**

- **WS1 — audit HIGHs** from the 2026-05-17 audit (PI cache-transport api-key-in-key
  / hop-by-hop headers, LE OB3-5, TG-N1, FE-D/E + SEC-1..4). Bounded security/hygiene;
  highest gain-per-effort; two are regression debt in recently-shipped cache code.
- **WS2 — operator/developer docs site** (NOT the end-user manual half — that is gated
  on the UI redesign settling). Aggregates existing in-repo Markdown; 4 sub-choices
  (hybrid-vs-thin, dir split, Pages-vs-RTD, domain) decided in the WS2 plan.
- **WS3 — migration-test-harness drift.** *Upgraded from "test-infra debt" to
  release-relevant:* the 2026-05-17 audit shows this drift is what lets a green suite
  mask **RB-1** (the `POST /api/review/sync` `ON CONFLICT` partial-index bug). Goal: a
  structural guard so a migration diverging from the init.sql snapshot is caught
  automatically, not patched per-number.

**Decided this session (decision recorded; finish-task pending):**

- **Installer (was "script-hardening vs desktop GUI") — RESOLVED.** Direction = **A+C**:
  a hardened `setup.sh` + the existing web first-run wizard *is* the GUI (the project's
  "all config via web UI" principle). **Native desktop app declined** (permanent
  signing/notarization/auto-update + security surface; breaks SSH/lab-server deploys;
  marginal gain over A+C). Finish-task carries an explicit **hardening + clear-docs
  acceptance gate** before it counts as done. Revisit native app only on evidence of
  users blocked specifically at "run one bootstrap line".
- **RB-3 cross-service auth divergence — DECIDED: Targeted convert (option 1).** Convert
  exactly the LE endpoints an orchestrator reaches — **`review/decks/cards/stats/projects`** —
  to the owner-override-capable resolver; document the rest of learning_engine as
  session-only by design; reconcile `architecture.md`'s "canonical" claim. Why this set:
  it is precisely what **Hermes** must orchestrate, so this also clears the Hermes
  prerequisite with no future redo. Bounded blast radius (vs option 3's 21-file
  regression risk) — correct for a stabilization cycle.
  - **When to escalate to Full Convergence (option 3 — one owner-override resolver
    everywhere, guard-enforced):** do it as its *own dedicated pass*, never inside a
    hardening cycle, triggered by any of — (a) a new orchestrator (beyond Telegram +
    Hermes) needs an LE endpoint *outside* the converted set; (b) the
    `check-no-unsafe-resolver` guard starts failing because new LE per-user endpoints
    are being added session-only (the documented boundary is eroding); (c) a cycle with
    explicit regression budget for the 21-file change. Until a trigger fires, the
    documented session-only boundary is the stable contract — do not pre-emptively
    converge.

**Sequenced future work (order matters — do not reorder casually):**

1. **Performance & hardware-fit — phased, own /deep-plan:**
   - *Phase 1 (do-next; hardware-agnostic, bounded):* profiling/bench harness
     instrumenting the hot paths (Pulse stage-2 scoring, embedding throughput,
     feed/facet queries, VRAM residency under concurrent load). Produces the evidence
     base for both Phase 2 *and* Hermes's perf budget. Highest-certainty next big-item.
   - *Phase 2 (deferred to target hardware):* vLLM-vs-Ollama spike behind the LiteLLM
     alias, with an explicit adopt-only-if kill criterion (Bucket-H measured-spike
     discipline — not automatic adoption). Cannot pay off on the CPU dev box.
   - *Phase 3:* per-detected-VRAM default-model recommendation + in-app hardware-fit
     guidance (reuses `setup.sh --check` GPU detection; no-yaml/env principle).
2. **Hermes conversational agent layer — CONDITIONAL GO, not now.** It is the
   natural-language front end to the 5 isolated capability surfaces (RAG,
   contradictions, citation/knowledge graph, Pulse, cards/review) — entirely net-new
   infra (no scaffolding exists today). Gate answer = GO, but **sequenced strictly
   after**: RB-3 (option 1) lands → Perf Phase 1 (so Hermes LLM fan-out regressions are
   isolable) → an **eval harness as task 0** (canned research-question set + expected
   tool sequences, or the agent is untestable). Orchestration substrate (thin custom
   loop vs **langgraph** vs **DSPy** for prompt-program optimization) is an explicit
   in-plan spike under the Bucket-H "no heavy dep without a measured spike" guardrail —
   the plan must not pre-assume langgraph. Realistic estimate ~20d, not 15.

**Conditional / latent (no action unless trigger fires):**

- **Qdrant corpus re-embed checkpoint** — only if the embedding model is ever changed
  (currently unchanged → not done, by design).
- **Full Convergence for RB-3** — see escalation triggers above.
- Remaining 2026-05-17 audit MEDIUM/LOW + SEC items not pulled into WS1 — next
  hardening cycle.

Resolved/moot (NOT deferred): "local-only push" posture (origin == master; GitHub CI
is the oracle); the "other-agent open work" (offline-sync endpoint / caddy crash /
micro-deferrals) verified ALREADY-SHIPPED at `5863ce5f` — do not re-flag. *Caveat:* the
2026-05-17 audit re-opened offline-sync as **RB-1** (a distinct `ON CONFLICT` SQL bug,
not the original "endpoint absent" finding) — that lives in WS1/WS3, not here.

See vault `~/ObsidianVault/projects/JARVIS_RD_Assistant/{open-questions,decisions}.md`
2026-05-17 entries.
