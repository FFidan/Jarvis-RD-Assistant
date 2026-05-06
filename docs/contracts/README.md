# JARVIS System Contracts

Evergreen reference documents that describe **what the system promises** at the
subsystem layer. Treat these as the canonical truth — when code disagrees with
a contract, fix one of them in the same patch.

## Why contracts exist (and how they differ from specs)

- **Specs** (`docs/specs/`) describe a *transition*: how the system changes
  between releases. They are dated, one-off, and become historical reference
  after their work ships.
- **Contracts** (this directory) describe the *steady state*: what every
  participant — code, test, agent, future maintainer — can assume about a
  subsystem. They are LIVING documents, updated whenever code changes break
  an invariant.

A contract that goes stale is worse than no contract. Every PR that changes a
contract-bound surface MUST update the contract in the same commit.

## Contracts in this set

| # | Contract | Scope |
|---|---|---|
| 01 | [Settings](01-settings.md) | Every user-controllable setting (`user_config` keys + per-table) — what stores it, what reads it, current LIVE/GHOST status |
| 02 | [Pulse](02-pulse.md) | The Pulse pipeline — stages, signals, weights, timeouts, fallback semantics |
| 03 | [LLM](03-llm.md) | LLM call choke point — the public surface, per-site contracts, retry/fallback policy |
| 04 | [Observability](04-observability.md) | Trace boundary policy, span types, privacy, sampling (forward-looking; B.2 not yet integrated) |

## How to read a contract

Each contract follows the same skeleton:

1. **What this covers (and does NOT)** — scope boundary
2. **Storage / data model** or equivalent foundation
3. **Behavioral promises** — per stage / per call site / per endpoint
4. **Failure modes** — degraded vs fatal; recoverable vs not
5. **Invariants** — assertable claims the code MUST satisfy
6. **Status table** — LIVE / GHOST / PARTIAL / DEPRECATED for every cited identifier
7. **Cleanup decisions deferred** — lists ghosts; does NOT prescribe fixes (that's the impl plan's job)
8. **Cross-contract references** — pointers to sibling contracts
9. **Verified Identifiers** — every cited symbol with `file:line — one-line behavior`, per the [grounding rule](../../CLAUDE.md)

## Status meanings

| Status | Meaning |
|---|---|
| **LIVE** | Written by code, read by code, behavior reaches end-user. |
| **GHOST** | Written by code (often by a UI control), but NEVER read. Configurable but inert. Decision pending: WIRE-IT or DELETE-IT. |
| **PARTIAL** | Read in only some code paths, or read once at startup and not refreshed, or only consumed by a non-core endpoint (e.g., test-connection). |
| **DEPRECATED** | Was LIVE in an earlier release; superseded but kept for backwards compatibility. Will be deleted in a named future cleanup. |

## Cross-references

- [docs/ARCHITECTURE.md](../ARCHITECTURE.md) — service and runtime boundaries (high-level)
- [docs/ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md) — durable engineering rules
- [docs/specs/archive/2026-04-29-paper-lifecycle-redesign.md](../specs/archive/2026-04-29-paper-lifecycle-redesign.md) — archived atomic-cutover spec; the lifecycle schema referenced by 01-settings.md
- [docs/specs/archive/2026-05-02-instructor-langfuse-integration.md](../specs/archive/2026-05-02-instructor-langfuse-integration.md) — archived B.1+B.2 implementation spec; transitional counterpart to 03-llm.md and 04-observability.md
- [docs/plans/archive/2026-04-30-marathon-meta.md](../plans/archive/2026-04-30-marathon-meta.md) — archived multi-phase modernization roadmap
