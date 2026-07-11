# JARVIS System Contracts

Evergreen reference documents that describe **what each subsystem promises** at
its interface boundary. They are living documents: when code changes a
contract-bound surface, update the contract in the same commit. A contract that
goes stale is worse than no contract.

## Contracts in this set

| # | Contract | Scope |
|---|---|---|
| 01 | [Settings](01-settings.md) | Every user-controllable setting (`user_config` keys + typed tables) — what stores it, what reads it, validators, and side-effects on write. |
| 02 | [Pulse](02-pulse.md) | The overnight Pulse pipeline — stages, signals, weights, timeouts, fallback semantics, diagnostics. |
| 03 | [LLM](03-llm.md) | The LLM call choke point — public surface, per-site contracts, retry/fallback policy, anti-hallucination integration. |
| 04 | [Observability](04-observability.md) | Langfuse trace-boundary policy, span types, privacy rules, opt-in posture, headless provisioning. |
| 05 | [Models and Hardware](05-models-and-hardware.md) | Curated model catalog, hardware-aware recommendations, pull/delete lifecycle, active defaults, and per-machine VRAM-fit / context controls. |
| 07 | [Testing](07-testing.md) | The four legitimate test shapes, the four prohibited anti-patterns, the carve-out registry, and the rot-on-touch policy. |

Numbering has a gap: contract 06 (hardware-aware settings) was consolidated into 05 and 07.

## How to read a contract

Each contract follows roughly the same skeleton: scope boundary → storage / data
model → behavioral promises (per stage / call site / endpoint) → failure modes →
invariants → cross-contract references → a Verified Identifiers table mapping each
cited symbol to `file:line`.

## Cross-references

- [docs/ARCHITECTURE.md](../ARCHITECTURE.md) — service and runtime boundaries.
- [docs/ENGINEERING_STANDARDS.md](../ENGINEERING_STANDARDS.md) — durable engineering rules (including typography and LLM-prompt-shape conventions).
