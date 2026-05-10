# Agentic Workflow

This document describes how agents should work in this repository.

Related docs:

- [../AGENTS.md](../AGENTS.md) - injected guide and truth hierarchy.
- [../CLAUDE.md](../CLAUDE.md) - local command checklist.
- [ARCHITECTURE.md](ARCHITECTURE.md) - runtime and package boundaries.
- [ENGINEERING_STANDARDS.md](ENGINEERING_STANDARDS.md) - coding and testing standards.
- [known-residual-risks.md](known-residual-risks.md) - accepted risks to preserve or revisit.
- [plans/2026-04-29-refreshed-desloppify-score-work.md](plans/2026-04-29-refreshed-desloppify-score-work.md) - current manual
  Desloppify score-work plan.

## Evidence Rules

- Read live files before citing existing behavior, writing tests against it, or
  putting identifiers into plans.
- Treat prior plans, memory, and Desloppify output as leads until verified.
- Plans and audit reports that cite existing symbols must end with a
  `## Verified Identifiers` table containing `file:line` and one-line behavior.
- If a plan lacks verified identifiers, re-read every cited identifier before
  acting on it.

## Repository Orientation

1. Read `CLAUDE.md` and `AGENTS.md`.
2. Read `graphify-out/GRAPH_REPORT.md` if present.
3. Read relevant system-of-record docs:
   - [docs/ARCHITECTURE.md](ARCHITECTURE.md)
   - [docs/ENGINEERING_STANDARDS.md](ENGINEERING_STANDARDS.md)
   - [docs/PRD.md](PRD.md)
   - [docs/known-residual-risks.md](known-residual-risks.md)
4. Inspect the current code and tests for the touched workflow.

## Subagents

Use subagents only when the user or active skill authorizes delegation. Before
dispatching, gather context yourself and provide:

- exact paths and line anchors
- relevant architecture constraints
- known risks
- disjoint write scope
- acceptance criteria
- verification commands

If a subagent reports blocked tooling, inspect the denial chain and decide the
next action in the parent session.

## Desloppify

Desloppify is useful for finding debt clusters, but it is not the release truth.

Required workflow for stale queues:

1. Check open issue paths against live files.
2. Mark only verified false positives.
3. Run a forced scan when stale/missing paths contaminate the queue.
4. Rerun holistic review before planning subjective score work.
5. Run staged triage or record manual triage if the runner cannot validate its
   own output.
6. Plan source refactors only from the refreshed queue.

Known verified false-positive patterns:

- `orphaned::libs/jarvis_common/.*` - shared editable-installed library.
- `security::frontend/node_modules/.*` - excluded vendor files.

Do not bulk-skip `test_coverage::services/.*` without live path evidence.

## Documentation Hygiene

`AGENTS.md` is an injected harness map, not the project manual. Keep long-lived
content in `docs/`.

Forbidden in `AGENTS.md`:

- current audit ledgers
- migration-count snapshots
- shipped-work chronology
- full directory trees
- legacy service app-layout paths from before the package rename

Run `python3 scripts/check_agent_docs.py` after editing harness docs.

### Silent-Revert Guard

Agents editing files through pre-commit hooks must verify the commit succeeded
before reporting done. The `ruff-format` hook rewrites `.py` files on disk but
marks itself `FAILED`; this leaves the git index diverged from the working tree.
A subsequent `git add -u` in a new shell invocation may silently drop the edit.
Root-cause analysis: [docs/agentic/format-watcher-rca.md](agentic/format-watcher-rca.md).
Backstop: `scripts/verify-edits.sh` performs post-commit blob verification.

Steps to avoid silent reverts:

1. After any `Edit` that may be affected by a formatter, run `git diff --name-only HEAD`.
2. After `git commit`, check the exit code and look for "1 changed" in the summary.
3. If the commit was aborted by a hook, the index still has the old blob. Re-stage
   the file with `git add <path>` (not `git add -u`) and recommit.

## Closeout

Before claiming completion:

- run the targeted verification commands
- report any commands that could not run and why
- summarize files changed and residual risks
- keep stale-plan or tool-validation caveats explicit
- if paper lifecycle or feed filtering was touched, verify against
  [docs/specs/archive/2026-04-29-paper-lifecycle-redesign.md](specs/archive/2026-04-29-paper-lifecycle-redesign.md)
  (the authoritative spec post-2026-04-29; supersedes the legacy
  `paper-lifecycle-contract.md` + `feed-information-architecture.md` which
  are scheduled for deletion in Phase A implementation)
