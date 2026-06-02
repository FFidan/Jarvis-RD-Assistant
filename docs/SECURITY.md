# Security Notes

Operational security guidance for JARVIS RD Assistant deployments.

---

## Threat Model

### Three Identities

JARVIS uses three distinct credential types with strictly bounded authority:

| Identity | Credential | Scope | Accesses user research data? |
|---|---|---|---|
| **Ops** | `JARVIS_API_KEY` (X-API-Key header) | Service-to-service calls (Telegram bot, cron jobs, health checks) | No — ops callers get no `user_id` from the session layer; user-data routes reject them with 401 |
| **User** | Magic-link session cookie (`jarvis_session`) | Own data only — the session layer injects `user_id` into every request; `current_user_id_strict` enforces it | Yes — own rows only |
| **Admin** | Session cookie with `role = admin` | Manages users via `/admin/users`, views the audit log at `/api/admin/audit-log`; **no access to other users' research data** | No cross-user data access — admin role controls ops, not data |

The `JARVIS_API_KEY` is an ops secret, not a user password. Anyone who holds it can call service endpoints but cannot read or write another user's papers, cards, or settings — the user-data layer requires a valid session identity.

The session layer includes a deliberate 24-hour `SESSION_GRACE` window (in `session_middleware.py`): a session expired by no more than 24 hours still resolves the user's identity (without renewing `expires_at`) so that reviews captured offline can reconcile after a realistic offline gap. This is an intentional offline-tolerance design choice, not a misconfiguration. `revoked_at` and `deleted_at` still hard-fail immediately regardless of the grace window — explicit revocation is never relaxed.

### In-Scope Attackers

- Unauthenticated network-level attacker (open dashboard port, no session or key).
- Authenticated user attempting to access another user's data (IDOR, horizontal escalation).
- Admin attempting to read or mutate another user's research data.
- Attacker with write access to the `pulse_models` table attempting RCE via pickle deserialization.

### Out of Scope (acknowledged residual risks)

- Attacker with host-level OS access (Docker socket, filesystem, or env).
- Attacker with direct PostgreSQL superuser access.
- Physical access to the host.
- Supply-chain compromise of upstream images or Python packages.

See `docs/known-residual-risks.md` for the full residual-risk register.

---

## Data Sharing Boundary

This section states the committed data-sharing model for a JARVIS instance.
The enforcement point is `assert_paper_ownership` / `assert_papers_ownership`
in `libs/jarvis_common/jarvis_common/db_helpers.py`.

### What is shared (corpus layer)

The following data is **shared across all authenticated users on an instance**:

- Paper metadata (title, authors, abstract, publication date, DOI, source).
- Full-text chunks and embeddings stored in Qdrant.
- Citation graph edges and knowledge-graph entities derived from those papers.
- Papers discovered by instance-level feeds: Pulse recommender, background
  scheduler, and Zotero group syncs.  These have ``papers.discovered_by IS NULL``
  (audit column only; no functional role).

A paper with ``discovered_by IS NULL`` is a system/instance paper and is
accessible to every authenticated user without requiring a ``user_library``
membership row.  This reflects instance configuration (feed settings, Pulse
model), not any individual user's behavior.

### What is strictly per-user (activity/output layer)

The following is **never cross-visible** — every query is scoped to
``user_id`` and no cross-user join is permitted:

- Library membership (``user_library`` rows).
- Paper read-state and ratings.
- Notes and annotations.
- Flashcards and card decks.
- Projects, tasks, and project–paper associations.
- Daily intent and Pulse preference signals.
- Structured extractions-of-record.
- Magic-link identity, session cookies, and user config values.

The corpus is a shared resource; the intellectual work on top of it is private
(comparable to a shared scholarly library with per-user workspaces).
Regression coverage lives in
`libs/jarvis_common/tests/test_ownership_canonical_invariant.py`.

---

## Dev Flags and Production Refusal

JARVIS has five granular development flags. When `DEV_MODE=true`, all five are
promoted to `true` unless overridden individually (see `settings.py`
`_promote_dev_flags`). **None are permitted in `ENVIRONMENT=production`** — the
service refuses to start if any is `true` at boot (`validate_production_config`
in `auth.py`).

| Flag | Effect when `true` (the protection it removes) |
|---|---|
| `DEV_AUTH_BYPASS` | All authentication is bypassed when no `JARVIS_API_KEY` is configured (no key and no session required). |
| `DEV_ERROR_DETAIL` | Raw exception detail is included in API error responses (information leakage). |
| `DEV_CORS_OPEN` | `Access-Control-Allow-Origin` is opened to `*` instead of the `CORS_ORIGINS` allowlist. |
| `DEV_SMTP_LOG_ONLY` | Magic-link emails are written to stdout/logs instead of being delivered via SMTP. |
| `DEV_CRYPTO_RELAXED` | Fernet key validation and HMAC key entropy requirements are relaxed. |

`DEV_MODE=true` is a meta-flag: it promotes any of the five that were not
explicitly set in the environment. An explicit env var always wins.

---

## Secret Environment Variables

| Variable | Purpose | Required in production |
|---|---|---|
| `JARVIS_API_KEY` | Ops API key — gates all non-auth, non-health backend endpoints. Min 32 chars; enforced at startup. | Yes (startup refuses if absent or < 32 chars) |
| `JARVIS_CONFIG_KEY` | Fernet key for `user_config.encrypted_value` at-rest encryption. | Yes (startup refuses if absent) |
| `JARVIS_CONFIG_KEY_OLD` | Previous Fernet key — enables zero-downtime rotation via MultiFernet. Set during key rotation; remove after. | No (rotation only) |
| `JARVIS_MODEL_HMAC_KEY` | Dedicated HMAC-SHA256 key for Pulse classifier pickle signing (see below). Min 32 chars. | Yes (startup refuses if absent; derivation from `JARVIS_API_KEY` is refused in production) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | SMTP relay credentials for magic-link delivery. Without these (or when set to empty strings, which are silently ignored — see the SMTP entry in [known-residual-risks.md](known-residual-risks.md)), links fall back to container stdout. | Strongly recommended |
| `OWNER_OVERRIDE_ALLOWED_CIDRS` | Comma-separated CIDR allowlist for `X-Owner-User-Id` header (Telegram bot per-user orchestration). Defaults to loopback + Docker bridge (`127.0.0.0/8,172.16.0.0/12`). | No (default is safe) |

### X-Owner-User-Id Mechanism

The Telegram bot sends `X-Owner-User-Id: <user_id>` to route per-user API
calls. This header is only honored when all three guards pass:

1. A valid `JARVIS_API_KEY` is present on the request.
2. The source IP falls within `OWNER_OVERRIDE_ALLOWED_CIDRS`.
3. The supplied `user_id` exists in the `users` table and is not deleted.

Any guard failure returns 403. The mechanism is implemented in
`current_user_id_strict_with_owner_override` in `libs/jarvis_common/jarvis_common/auth.py`.

### Config Key Rotation

To rotate `JARVIS_CONFIG_KEY` without downtime:

1. Set `JARVIS_CONFIG_KEY_OLD=<old key>` and `JARVIS_CONFIG_KEY=<new key>`.
2. Run `scripts/rotate_config_key.py --apply` (dry-run first without `--apply`).
3. Restart services.
4. Remove `JARVIS_CONFIG_KEY_OLD` from the environment.

---

## Proxy-Trust and Source-IP Allowlisting

Two auth surfaces use `request.client.host` to determine source IP for
allowlist checks. The reported IP is rewritten by `ProxyHeadersMiddleware`
when the request originates from a host in `trusted_proxy_hosts` (see
`configure_middleware_and_errors` in
`libs/jarvis_common/jarvis_common/app_factory.py`). If `trusted_proxy_hosts`
is broader than the actual reverse-proxy fleet, an attacker behind any
included host could spoof `X-Forwarded-For` to forge the source IP.

The IP-allowlist call sites are:

- `_ip_in_allowlist` (`libs/jarvis_common/jarvis_common/auth.py`) — backs
  `OWNER_OVERRIDE_ALLOWED_CIDRS` for the `X-Owner-User-Id` header bypass;
  mis-trusted XFF forges the operator's IP guard.
- `_infra_ip_in_allowlist`
  (`services/paper_ingestion/paper_ingestion/routers/infra_events.py`) —
  backs `INFRA_INGEST_ALLOWED_CIDRS` for the Vector sidecar ingest
  endpoint; mis-trusted XFF forges the sidecar's IP and reduces the
  defense-in-depth to the HMAC challenge alone.

**Deployment requirement:** keep `trusted_proxy_hosts` scoped to the
actual reverse-proxy host(s) — in this stack that is the Caddy container's
bridge IP only. Do NOT set `trusted_proxy_hosts="*"` in any production
deployment. The setting is exposed via `TRUSTED_PROXY_HOSTS` in
`CoreSettings`; the default value is `dashboard` (the Caddy reverse-proxy
service), which is correct for the standard single-host stack. Override only
when deploying behind a different proxy fleet.

---

## Audit Log

Security-relevant events are written to the `audit_log` table by
`libs/jarvis_common/jarvis_common/audit.py`. The audit log is best-effort
(failures are logged but do not abort the request) and caps metadata payloads at
4 KB to prevent log inflation.

Events currently audited include: invalid API key attempts, missing session on
user-data routes, magic-link request and verify, user creation and deletion,
admin actions.

The audit log is readable by admins at `GET /api/admin/audit-log`
(cursor-paginated, admin session required).

---

## Vulnerability Disclosure

If you discover a security vulnerability in JARVIS RD Assistant, please report
it responsibly:

**Contact:** Use [GitHub Security Advisories](https://github.com/FFidan/Jarvis-RD-Assistant/security/advisories/new) (private vulnerability reporting).

**Process:**
1. Open a private security advisory on GitHub with a description of the
   vulnerability, steps to reproduce, and any proof-of-concept.
2. Allow up to **90 days** for a fix to be developed and released before public
   disclosure (coordinated disclosure).
3. Acknowledgement will be sent within 5 business days of receipt.

Please do not open public GitHub issues for security vulnerabilities.

---

## Pulse Model Signing

The Pulse classifier persists a serialized scikit-learn `LogisticRegression`
model into the `pulse_models` table as an HMAC-signed pickle blob. Verification
happens in `services/paper_ingestion/paper_ingestion/pulse/training.py::_verify_and_unpickle`
before `pickle.loads` is called — without the HMAC gate, anyone with DB write
access could forge a blob and trigger RCE.

### Configuration

The HMAC key is resolved at call time, in this order:

1. **`JARVIS_MODEL_HMAC_KEY`** (preferred) — a dedicated secret used solely for
   signing model blobs. Generate with `openssl rand -hex 32`. Keeping this
   separate from `JARVIS_API_KEY` means a compromise of the HTTP bearer does
   not also let an attacker forge model blobs, and vice versa.
2. **Derived from `JARVIS_API_KEY`** — when `JARVIS_MODEL_HMAC_KEY` is unset,
   the signing key is `sha256(b"model-signing:" + JARVIS_API_KEY)`. The
   `model-signing:` prefix domain-separates this key from any direct use of
   the bearer.

If neither is set, `_hmac_key()` raises `RuntimeError`. In production
(`ENVIRONMENT=production`), `validate_production_config()` — called at lifespan
startup — refuses to start unless at least one of the two paths above is
configured.

### Key Rotation

There is no in-place rotation framework. To rotate the HMAC key:

1. Update `JARVIS_MODEL_HMAC_KEY` and restart the affected services.
2. Existing `pulse_models` rows will fail HMAC verification and the scoring path
   falls back to zeros until a new model is trained.
3. The nightly `pulse.train_classifier` cron job re-trains automatically. To
   force an immediate re-train, enqueue `pulse.train_classifier` via the jobs API.

---

## Ollama Security Posture

The Ollama daemon handles local LLM inference. Key constraints:

- **Image pin:** keep `OLLAMA_IMAGE` in `versions.env` at the tested pin
  (`ollama/ollama:0.23.1`) or a newer validated pin. Downgrading below the
  patched line reintroduces known vulnerabilities (see
  [known-residual-risks.md](known-residual-risks.md) for the current CVE
  posture entry).
- **Host binding:** the default Compose configuration binds the Ollama host
  port to loopback only (`127.0.0.1`), preventing browser and LAN clients from
  calling the daemon directly.
- **Docker network boundary:** every container on the `jarvis` Docker network
  can reach `http://ollama:11434`. Do not attach untrusted sidecars to that
  network.
- **Shared daemon override:** if the operator configures an external shared
  Ollama daemon instead of the bundled one, that daemon must be patched to an
  equivalent or newer pin and bound to loopback or an equivalently trusted
  private network.

Review this posture whenever `OLLAMA_IMAGE` is updated or the Compose network
topology changes.
