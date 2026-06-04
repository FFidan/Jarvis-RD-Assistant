# JARVIS RD Assistant — Operator & Developer Docs

Self-hosted multi-tenant research assistant. This site covers deployment,
architecture, engineering standards, security, and API contracts for operators
and developers.

---

## Get Started

| Document | Purpose |
|---|---|
| [Quickstart / README](readme-shim.md) | Clone, onboarding wizard, env vars |
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
| [Migrations](migrations-shim.md) | Migration convention, runner, multi-tenant history |

## System Contracts

| Document | Purpose |
|---|---|
| [Overview](contracts/README.md) | Evergreen subsystem invariants |
| [Settings](contracts/01-settings.md) | Settings schema and validation |
| [Pulse](contracts/02-pulse.md) | Pulse generation pipeline |
| [LLM Client](contracts/03-llm.md) | LLM call pattern, models, structured output |
| [Observability](contracts/04-observability.md) | Langfuse tracing, health endpoints |
| [Models & Hardware](contracts/05-models-and-hardware.md) | Model adoption gates, VRAM tiers, GPU detection |
| [Testing](contracts/07-testing.md) | Contract testing framework and patterns |

## User Guide

| Document | Purpose |
|---|---|
| [User Guide](manual/index.md) | End-user manual — sign-in, features, Telegram integration |

---

> Canonical edit targets are the Markdown files in the repository.
> This site is a generated read-only view — always edit the source, not the HTML.
