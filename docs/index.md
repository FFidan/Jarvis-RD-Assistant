# JARVIS RD Assistant — Operator & Developer Docs

Self-hosted multi-tenant research assistant. This site covers **deployment,
architecture, engineering standards, security, and API contracts** for operators
and developers. End-user documentation is not included here.

---

## Quick links

| Document | Purpose |
|---|---|
| [Quickstart / README](readme-shim.md) | Clone, first-run wizard, env vars |
| [Deployment Guide](DEPLOYMENT.md) | Solo, LAN, Cloudflare Tunnel, TLS, backups |
| [Architecture](ARCHITECTURE.md) | Service topology, runtime boundaries, data flows |
| [Engineering Standards](ENGINEERING_STANDARDS.md) | Coding conventions, API rules, DB rules, testing |
| [Security Notes](SECURITY.md) | Threat model, auth, secrets rotation, hardening |
| [Agentic Workflow](AGENTIC_WORKFLOW.md) | Evidence rules, Desloppify, agent closeout |
| [Migrations](migrations-shim.md) | Migration convention, runner, multi-tenant history |
| [System Contracts](contracts/README.md) | Evergreen subsystem invariants |

---

## Scope

This site publishes only the **operator/developer half** of the documentation.
The end-user manual is gated on the UI redesign and not published here.

> Canonical edit targets are the Markdown files in the repository.
> This site is a generated read-only view — always edit the source, not the HTML.
