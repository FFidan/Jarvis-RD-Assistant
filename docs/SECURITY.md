# Security Notes

Operational security guidance for JARVIS RD Assistant deployments.

---

## Threat Model

### Three Identities

JARVIS uses three distinct credential types with strictly bounded authority:

| Identity | Credential | Scope | Accesses user research data? |
|---|---|---|---|
| **Ops / owner recovery** | `JARVIS_API_KEY` (X-API-Key header) | Service-to-service calls and a gated session exchange for the configured instance owner | Only after the owner exchange mints that administrator's normal scoped session; the bare key has no user identity |
| **User** | Magic-link session cookie (`jarvis_session`) | Own data only — the session layer injects `user_id` into every request; `current_user_id_strict` enforces it | Yes — own rows only |
| **Admin** | Session cookie with `role = admin` | Manages users via `/admin/users`, views the audit log at `/api/admin/audit-log`; **no access to other users' research data** | No cross-user data access — admin role controls ops, not data |

The `JARVIS_API_KEY` is an operations secret, not a family password. It can
mint a normal session for the configured owner on localhost or HTTPS, including
on a multi-user instance. It cannot mint sessions for other users. User-data
routes still require the identity carried by a session.

The session layer uses **rolling (sliding) expiry** (`session_middleware.py`). A signed-in session is valid for 30 days (`SESSION_TTL`); each time it is used its expiry rolls forward another 30 days, but at most once per day (`SESSION_RENEW_AFTER`) so a busy user triggers a single database write per day rather than one per request. Renewal is a single atomic `UPDATE` whose `WHERE` clause is the security boundary: it renews only rows that are **not revoked** and **not already expired**, so neither a revoked session nor a grace-resolved one can extend itself. When a renewal happens the response re-issues the `Set-Cookie`, so the browser and the database row move together and never disagree on when the session ends.

Layered on top is a deliberate 24-hour `SESSION_GRACE` window: a session expired by no more than 24 hours still **resolves** the user's identity (without renewing `expires_at`) so that reviews captured offline can reconcile after a realistic offline gap. This is an intentional offline-tolerance design choice, not a misconfiguration. `revoked_at` and `deleted_at` still hard-fail immediately regardless of the grace window — explicit revocation and account deletion are never relaxed. A scheduled purge job deletes expired-and-revoked sessions and expired passkey challenges so neither table grows without bound.

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

## Passkeys (WebAuthn)

JARVIS supports passkey sign-in and per-device credential management alongside the magic-link flow.

- **Origin binding.** A named WebAuthn origin is derived from the
  operator-configured `APP_BASE_URL` and matched **exactly**; the server never
  trusts a request-derived origin, and the RP ID is the matched hostname. A
  configured non-loopback origin must be HTTPS. Local installs additionally
  allow only `localhost` and `jarvis.localhost`, on their served HTTP or HTTPS
  ports.
- **User verification (UV) is required** for both registration and login. A login that fails UV is rejected **without** revoking the credential, so a failed verification cannot be used to lock a user out of their own passkey.
- **Replay / one-finish.** Each challenge is claimed with a single `DELETE … RETURNING`, so a replayed or concurrently double-finished ceremony resolves exactly once.
- **Cloned-authenticator detection.** An authenticator presenting a signature counter that is nonzero and less-than-or-equal-to the stored value is treated as cloned: the credential is revoked and all of its sessions are ended in the same transaction. Authenticators that legitimately never increment their counter (counter stays 0) continue to work.
- **Revocation.** Deleting one credential ends only that credential's sessions;
  an admin revoke-all ends every passkey session. Recovery requires working
  email, a manual link from a signed-in administrator, or the owner-only API-key
  exchange. Operators should verify one of those paths before removing the last
  administrator passkey.
- **Availability signal.** A capability endpoint reports whether the current
  origin can run a passkey ceremony. Raw-IP LAN HTTP serves only
  `/health/jarvis`; every other remote path returns HTTP 403. Remote users need
  a named HTTPS origin.

---

## Source-aware paper visibility

Paper access is determined by the persisted `visibility_scope` and explicit
library membership. Provenance fields such as `source_type` and
`discovered_by` remain useful for audit and display, but they do not grant
access. An authenticated user can retrieve a paper only when it is persisted as
`public` or the user has explicitly added it to their own library.

| Origin | Persisted scope | Who can retrieve it? |
|---|---|---|
| **Verified server source** — arXiv, Semantic Scholar, OpenAlex, or PubMed through the server-owned adapter | Public | Every authenticated user on the instance |
| **Local PDF upload** | Private | Only users who explicitly add the paper to their library |
| **Unverified client batch** — including request-supplied citation metadata | Private | Only users who explicitly add the paper to their library |
| **Personal or group Zotero** import | Private | Only the connecting user unless another user independently adds the same paper |
| **Ambiguous legacy Zotero** identity | Private | Excluded from other users; it remains unlinked until a user explicitly shelves it or its namespace can be resolved safely |
| **Unknown or unattributed** provenance | Private | Excluded unless a user explicitly adds the paper to their library |

The same rule applies to feed results, direct paper reads, citations, graph
views, summaries, and single- or cross-paper Ask. Full-text chunks and vectors
do not widen it: authenticated vector retrieval also requires the current
visibility generation, and the relational database rechecks every candidate.
Missing or stale generation metadata therefore causes temporary under-fetch,
never broader access.

Private activity and output remain user-scoped regardless of a paper's
visibility: library membership, read state, ratings, notes, annotations,
flashcards, projects and tasks, Pulse preferences, structured extractions,
account settings, and sessions are never shared between users. Administrators
manage the deployment but do not gain access to another user's private research
through their role.

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
| `DEV_SMTP_LOG_ONLY` | SMTP delivery is suppressed. Logs and the event stream receive only a SHA-256 recipient hash and non-secret status metadata; the address, bearer link, and token are never logged. |
| `DEV_CRYPTO_RELAXED` | Fernet key validation and HMAC key entropy requirements are relaxed. |

`DEV_MODE=true` is a meta-flag: it promotes any of the five that were not
explicitly set in the environment. An explicit env var always wins.

---

## Secret Environment Variables

| Variable | Purpose | Required in production |
|---|---|---|
| `JARVIS_API_KEY` | Ops API key — gates all non-auth, non-health backend endpoints. Min 32 chars; enforced at startup. | Yes (startup refuses if absent or < 32 chars) |
| `JARVIS_CONFIG_KEY` | Fernet key for `user_config.encrypted_value` at-rest encryption. | Yes (startup refuses if absent) |
| `JARVIS_MODEL_HMAC_KEY` | Dedicated HMAC-SHA256 key for Pulse classifier pickle signing (see below). Min 32 chars. | Yes (startup refuses if absent; derivation from `JARVIS_API_KEY` is refused in production) |
| `LITELLM_SALT_KEY` | Salt key LiteLLM uses to encrypt model credentials (cloud provider API keys) stored in its admin database. Auto-generated into `secrets/litellm_salt_key.txt` by `scripts/init-secrets.sh`; delivered as a Docker secret. **Never rotate** — LiteLLM decrypts stored credentials with this exact key, and without a dedicated salt LiteLLM falls back to the master key, so a master-key rotation would brick every encrypted row. | Yes (the litellm container refuses to start without it) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | SMTP relay credentials for automatic magic-link delivery. Without them, an administrator can still create a 24-hour invitation or 15-minute recovery link and share it privately. Explicit empty-string secret values are rejected; leave the fields unset when email is intentionally disabled. | Optional; recommended when users need self-service recovery |
| `SMTP_REPLY_TO` | Optional Reply-To address for sign-in emails. When set, email clients route replies here instead of the From address. Not a secret — this value appears in outgoing email headers. Configurable via the wizard or Settings → System → Email / SMTP. | No |
| `SMTP_FROM_NAME` | Optional sender display name shown in the From header (e.g. `JARVIS RD`). Not a secret — this value appears in outgoing email headers. Configurable via the wizard or Settings → System → Email / SMTP. | No |
| `OWNER_OVERRIDE_ALLOWED_CIDRS` | Comma-separated CIDR allowlist for the `X-Owner-User-Id` header (Telegram bot per-user orchestration). The compose stack sets this to loopback + the Telegram bot's own pinned address as a `/32` (tracks `JARVIS_TELEGRAM_BOT_IP`, which setup derives from `JARVIS_NET_SUBNET`; default `10.137.241.250`), so no other container on the bridge can send the header. The bare code default (`127.0.0.0/8`) is loopback-only (deny-by-default); non-loopback callers must opt in explicitly. | No (compose default is correct) |
| `ALLOW_PRIVATE_SMTP_HOST` | Default `false`. When `false`, the SMTP host is validated at config-save AND at magic-link send time and rejected if it resolves to a private/loopback/link-local/reserved address (SSRF guard). Set `true` ONLY if you run a legitimate **internal SMTP relay** on a private address/hostname — otherwise magic-link delivery to that relay is refused. | No (set only for an internal relay) |

### Cloud LLM Provider Settings at Rest

Cloud provider settings entered in Settings are deployment-wide admin settings.
Existing providers keep their backward-compatible flat keys (`llm.anthropic.api_key`,
`llm.openai.api_key`, `llm.google.api_key`); newer providers use namespaced keys
such as `llm.providers.openrouter.api_key`, `llm.providers.deepseek.api_key`,
`llm.providers.mistral.api_key`, `llm.providers.moonshot.api_key`,
`llm.providers.zai.api_key`, and `llm.providers.custom_openai_compatible.api_key`.
The custom OpenAI-compatible endpoint also stores
`llm.providers.custom_openai_compatible.base_url`.

Provider keys live in TWO encrypted stores when assigned to a role:

1. **`user_config.encrypted_value`** — Fernet-encrypted under
   `JARVIS_CONFIG_KEY`. This is the source of truth; it survives LiteLLM
   resets and is what the UI masks/reads back.
2. **LiteLLM's admin database** (`litellm` DB in the bundled Postgres) — when
   a cloud model is assigned to a role, the key is decrypted in memory and
   carried in the `POST /model/new` payload; LiteLLM persists it in
   `LiteLLM_ProxyModelTable` encrypted under `LITELLM_SALT_KEY`.

Keys are never written to `.env`, `litellm/config.yaml`, container
environment blocks (visible via `docker inspect`), or any other file.
Rotating `JARVIS_CONFIG_KEY` (supported, see below) does not affect store 2;
`LITELLM_SALT_KEY` itself must never rotate (see the table above).

Custom endpoint base URLs are validated before save. The validator rejects
credentials in the URL, fragments, invalid schemes, and unsafe IP ranges such
as metadata-service, link-local, multicast, and unspecified addresses. HTTPS is
allowed for normal remote endpoints; HTTP is limited to loopback development or
self-hosted gateway use. Administrators should only configure endpoints they
trust, because prompts and relevant source excerpts are sent there when a custom
model is assigned.

### X-Owner-User-Id Mechanism

The Telegram bot sends `X-Owner-User-Id: <user_id>` to route per-user API
calls. This header is only honored when all three guards pass:

1. A valid `JARVIS_API_KEY` is present on the request.
2. The source IP falls within `OWNER_OVERRIDE_ALLOWED_CIDRS`.
3. The supplied `user_id` exists in the `users` table and is not deleted.

Any guard failure returns 403. The mechanism is implemented in
`current_user_id_strict_with_owner_override` in `libs/jarvis_common/jarvis_common/auth.py`.

The bot resolves the `user_id` it injects from `telegram_user_pairings` — the
durable record written when a user runs `/pair <token>` in the bot. This is
the bot's sole identity mechanism; it does not rely on a fixed chat-id
environment variable. The `telegram.owner_chat_id` config key is active: the scheduler reads it to
resolve the deployment owner's Telegram chat ID for timezone-based nudge
scheduling and job failure-alert delivery (see
`services/telegram_bot/telegram_bot/scheduler.py`).

The bot's product-data tenant isolation (projects, tasks, milestones, papers,
author alerts) is enforced entirely server-side by the service endpoints it
calls — the same routes the web app uses. The bot is a REST caller, not a
second writer implementing its own scoping.

### Config Key Rotation

The standard Docker deployment rotates `JARVIS_CONFIG_KEY` during a short
maintenance window. The old and new keys are mounted into a one-off application
container as files; `scripts/rotate_config_key.py` first validates every row and
then re-encrypts them in one database transaction. Run
`bash scripts/rotate-config-key.sh` from the repository root: the wrapper updates
both the Docker secret file and `.env`, waits for healthy services, and can
resume safely after an interruption. Follow [Encrypted config key
rotation](DEPLOYMENT.md#encrypted-config-key-rotation); do not replace only one
of those two files by hand.

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
  defense-in-depth to the static shared key (`INFRA_INGEST_KEY`) alone.
  Note: `/infra-events` authenticates with a static shared key (constant-time
  `hmac.compare_digest`) plus the CIDR allowlist — it is NOT a challenge-response
  scheme and has no replay/nonce protection; the internal-network + default-deny
  CIDR posture is the boundary.

**Deployment requirement:** keep `trusted_proxy_hosts` scoped to the actual
reverse-proxy hop. Do not set `trusted_proxy_hosts="*"` in production. In the
standard stack, the application trusts only loopback and the dashboard
container's derived `/32` address. Nginx, in turn, accepts forwarded HTTPS
metadata only on its separate trusted-ingress listener and only from the
derived host gateway, Caddy, local Caddy, and Cloudflare Tunnel addresses.
The public/LAN listener discards browser-supplied forwarding metadata.
Cloudflare client addresses are honoured only when the request arrives on the
trusted listener from the pinned `cloudflared` container and nginx marks that
ingress as Cloudflare-owned. Direct and LAN callers cannot opt in by sending
`CF-Connecting-IP` or `X-Forwarded-For` themselves.

| Boundary | Mapping | Remote behavior |
|---|---|---|
| Raw/local listener | container `3000`, host `3001` | Localhost gets the app; LAN clients get only `/health/jarvis`, with HTTP 403 everywhere else |
| Trusted-edge listener | container `3002` | Only the derived gateway and pinned TLS edge containers are accepted |
| Tailscale host ingress | host loopback `127.0.0.1:3003` to container `3002` | Tailscale Serve terminates HTTPS before forwarding |

These values are numeric because uvicorn matches the immediate socket peer
before applying `X-Forwarded-For`. Setup and update accept an IPv4 `/27` or
larger network and derive the gateway and highest five usable addresses from
`JARVIS_NET_SUBNET`; a custom subnet does not require Compose or nginx edits.
Nginx also strips any browser-supplied `X-Owner-User-Id` header before proxying;
only the container-internal caller that does not traverse nginx can set it.
Override the defaults only for a proxy topology whose exact peers you control.

### Transport header scope

The loopback mkcert edge sends `Strict-Transport-Security: max-age=0` to clear
rather than enable HSTS, because browsers store that policy for the `localhost`
hostname across ports. A positive policy from port 3443 would break the supported
plain-HTTP fallback on port 3001. The public Caddy edge sets a one-year policy
for its configured hostname only; it does not claim sibling hostnames or browser
preload. Operators who make either broader commitment must first ensure that
every affected hostname already supports HTTPS.

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

### Append-only invariant and GDPR erasure

`audit_log` is append-only: the `no_update_audit_log` and `no_delete_audit_log`
RULEs rewrite any ordinary `UPDATE`/`DELETE` into a no-op, so the operational
record cannot be tampered with after the fact. Because `audit_log.user_id` is a
free-text column (not a foreign key), hard-deleting a user does not cascade to
it. To honour GDPR erasure, the daily purge (`jobs/data_purge.py`) anonymizes a
hard-deleted user's audit rows — nulling `user_id` and stripping PII metadata
keys (e.g. `ip`) while keeping the non-identifying operational record (action,
resource, timestamp). This is the one sanctioned mutation: it brackets the
`UPDATE` with `ALTER TABLE … DISABLE/ENABLE RULE no_update_audit_log` in a single
transaction (RULEs are query-rewrite and role-independent, so `SECURITY DEFINER`
cannot bypass them); the rule is re-enabled before commit, so ordinary writes
remain no-ops.

---

## Vulnerability Disclosure

Reporting a vulnerability is documented in the repository's top-level
[`SECURITY.md`](https://github.com/limitcycle-oss/jarvis-rd-assistant/blob/main/SECURITY.md)
policy (private GitHub Security Advisories;
acknowledgement within 5 business days). Do not open public GitHub issues for
security reports.

---

## Pulse Model Signing

The Pulse classifier persists a serialized scikit-learn `LogisticRegression`
model into the `pulse_models` table as an HMAC-signed pickle blob. Verification
happens in `services/paper_ingestion/paper_ingestion/pulse/training.py::_verify_and_unpickle`
before `pickle.loads` is called — without the HMAC gate, anyone with DB write
access could forge a blob and trigger RCE.

### Configuration

The HMAC key is resolved at call time:

1. **`JARVIS_MODEL_HMAC_KEY`** — a dedicated secret used solely for
   signing model blobs. Generate with `openssl rand -hex 32`. Keeping this
   separate from `JARVIS_API_KEY` means a compromise of the HTTP bearer does
   not also let an attacker forge model blobs, and vice versa. This dedicated
   key is mandatory in production and in every multi-user deployment.
2. **Derived from `JARVIS_API_KEY`** — a backward-compatible fallback allowed
   only in single-user, non-production mode. The signing key is
   `sha256(b"model-signing:" + JARVIS_API_KEY)`; the prefix domain-separates it
   from direct use of the bearer.

If the allowed key is absent, `_hmac_key()` raises `RuntimeError`. Production
startup and the multi-user gate refuse a missing or short dedicated key; they
never accept the derived fallback.

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
  (`ollama/ollama:0.31.2`) or a newer validated pin. Downgrading below the
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

---

## Restore boundary

The application does not perform database restores. An active administrator
must type **RESTORE**; the app then writes a request to the `backup_trigger`
volume. The `postgres-backup` sidecar takes the safety backup, verifies the
selected restore point, loads staging databases, and performs the swap. The app
mounts backup archives read-only, and the operations API key cannot start a
restore.

A failure before the swap leaves the live databases in place. Once the swap
begins, a failure keeps the service in maintenance until the operator recovers
from the safety restore point. Restoring a point from a newer application
version is refused.

A compromised admin session can still request a destructive rollback. Typed
confirmation and the safety point reduce accidents, not a deliberate admin
action. See the [risk register](known-residual-risks.md).

### Fresh-host uploads

Browser uploads go to the dedicated `restore-uploader` container, not the
application. It can write only to `restore_inbox` and can read only the hashed,
expiring upload grant from `backup_trigger`. It has no database credentials,
Docker socket, or host-secret mount. A restore still requires the separate
admin confirmation.

The backup sidecar checks the signed archive set and proves that the supplied
key can decrypt it before the swap. It removes the one-time key and plaintext
staging on normal and recorded-failure exits; a forced `SIGKILL` can delay that
cleanup until the next run. Same-host restores do not use the inbox key. The
[backup and restore guide](manual/backup-and-restore.md) contains both browser
and headless recovery procedures.
