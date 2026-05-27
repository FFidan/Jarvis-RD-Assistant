# JARVIS RD Assistant — Operator & Developer Docs

Self-hosted multi-tenant research assistant. This site covers deployment,
architecture, engineering standards, security, and API contracts for operators
and developers.

---

## Get Started

| Document | Purpose |
|---|---|
| [Quickstart / README](readme-shim.md) | Clone, first-run wizard, env vars |
| [Deployment Guide](DEPLOYMENT.md) | Solo, LAN, Cloudflare Tunnel, TLS, backups |
| [Requirements](REQUIREMENTS.md) | Hardware, software, and dependency requirements |

## Operate

| Document | Purpose |
|---|---|
| [Security](SECURITY.md) | Threat model, auth, secrets rotation, hardening |
| [Release Process](RELEASE.md) | Versioning, changelog, release checklist |
| [Known Residual Risks](known-residual-risks.md) | Accepted risks, mitigations, deferred items |

## Develop

| Document | Purpose |
|---|---|
| [Architecture](ARCHITECTURE.md) | Service topology, runtime boundaries, data flows |
| [Product Requirements](PRD.md) | Feature scope, personas, acceptance criteria |
| [Engineering Standards](ENGINEERING_STANDARDS.md) | Coding conventions, API rules, DB rules, testing |
| [Typography Contract](contracts/08-typography.md) | UI text formatting invariants |
| [LLM Prompt Shape](contracts/09-llm-prompt-shape.md) | Prompt-construction contract enforced by AST check |
| [Migrations](migrations-shim.md) | Migration convention, runner, multi-tenant history |

## System Contracts

| Document | Purpose |
|---|---|
| [Overview](contracts/README.md) | Evergreen subsystem invariants |
| [Settings](contracts/01-settings.md) | Settings schema and validation |
| [Pulse](contracts/02-pulse.md) | Pulse generation pipeline |
| [LLM Client](contracts/03-llm.md) | LLM call pattern, models, structured output |
| [Observability](contracts/04-observability.md) | Langfuse tracing, health endpoints |
| [Model Lifecycle](contracts/05-model-lifecycle.md) | Model adoption gates and retirement |
| [Hardware-Aware Settings](contracts/06-hardware-aware-settings.md) | VRAM tiers, GPU detection |

## User Guide

End-user documentation lives under [User Guide](manual/index.md).

---

> Canonical edit targets are the Markdown files in the repository.
> This site is a generated read-only view — always edit the source, not the HTML.
